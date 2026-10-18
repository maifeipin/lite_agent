#!/usr/bin/env python3
"""
E0 阶段可复现验证门禁入口 (scripts/verify.py)
一键执行以下检查并输出结构化汇总报告：
1. 架构静态门禁 (AST check_architecture.py)
2. Agent 全量测试套件 (包含副作用隔离守卫与负向用例, sys.executable -m pytest)
3. Parser 跨仓测试套件 (包含路径隔离与边界测试, unittest)
4. 两仓 Git diff 规范校验 (git diff --check)
5. Docker Python 3.9 脱敏快照只读隔离环境验证:
   - Parser 全量 unittest (只读挂载 :ro, 禁用外网)
   - AST 架构门禁 (只读挂载 :ro, 禁用外网)
   - Agent 全量 pytest (只读挂载 :ro)
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, Any, Tuple


# 明确的代码与已核实脱敏 fixtures 白名单
ALLOWED_PARSER_ROOT_FILES = {
    'billing_readmodel.py', 'feishu_bot.py', 'mail_client.py', 'mail_connect.py',
    'mail_llm.py', 'mail_parse.py', 'mail_quality.py', 'mail_sync.py',
    'oauth_helper.py', 'parser_paths.py', 'replace_script.py', 'reprocess_failed.py',
    'statement_db.py', 'statement_evidence.py', 'statement_identity.py', 'statement_models.py', 'statement_parser.py',
    'noise_rules.json', 'email-config.example.json', 'feishu_config.example.json',
    'requirements.txt', 'README.md'
}

ALLOWED_PARSER_FIXTURES = {
    'cmb_due_source_missing.eml',
    'cmb_reviewed_statement.eml',
    'cmb_due_extraction_failed.eml',
    'cmb_due_source_unknown.eml',
}

ALLOWED_AGENT_ROOT_FILES = {
    'agent.py', 'session.py', 'security.py', 'main.py',
    'config/examples/agent.json', 'config/architecture/rules.json',
    'config/architecture/core-manifest.txt', 'config/pytest.ini',
    'requirements/core.txt', 'requirements/test.txt', 'requirements/full.txt',
    'config/docker/Dockerfile.test',
    'dev/config/examples/agent.json', 'dev/config/architecture/rules.json',
    'dev/config/architecture/core-manifest.txt', 'dev/config/pytest.ini',
    'dev/requirements/core.txt', 'dev/requirements/test.txt', 'dev/requirements/full.txt',
    'dev/config/docker/Dockerfile.test',
    'dev/__init__.py', 'lite_agent/__init__.py',
}

ALLOWED_AGENT_EXACT_FILES = {
    'tests/fixtures/contracts/canonical-files.json',
    'dev/tests/fixtures/contracts/canonical-files.json',
    'edge_node/whitelist.json',
    'scripts/whitelist.json',
    'scripts/rss_topic/paths.json',
    'tests/channels/wechat_ilink/fixtures/getupdates_success.json',
    'dev/tests/channels/wechat_ilink/fixtures/getupdates_success.json',
    'tests/channels/wechat_ilink/fixtures/sendmessage_success_octet_stream.json',
    'dev/tests/channels/wechat_ilink/fixtures/sendmessage_success_octet_stream.json',
    'tests/frontend/test_review_meeting_frontend.js',
    'dev/tests/frontend/test_review_meeting_frontend.js',
    'config/architecture/core-manifest.txt',
    'dev/config/architecture/core-manifest.txt',
    'scripts/export_core.py',
    'scripts/verify_core.py',
    'lite_agent/integrations/phone_app/worker/README.md',
}

ALLOWED_AGENT_CODE_DIRS = [
    'app', 'config', 'requirements', 'channels', 'contracts', 'core', 'edge_node', 'features',
    'integrations', 'memory_engine', 'services', 'skills', 'scripts',
    'tests', 'web_dashboard', 'conf.d', 'dev', 'lite_agent'
]

CORE_TEST_FILES = [
    "dev/tests/kernel/test_adaptive_budget.py",
    "dev/tests/kernel/test_agent_runtime.py",
    "dev/tests/architecture/test_check_architecture.py",
    "dev/tests/tooling/test_clean_core_environment.py",
    "dev/tests/kernel/test_config_loader_settings.py",
    "dev/tests/tooling/test_core_manifest_and_export.py",
    "dev/tests/kernel/test_cron_engine.py",
    "dev/tests/kernel/test_execution.py",
    "dev/tests/kernel/test_execution_budget.py",
    "dev/tests/kernel/test_execution_ledger.py",
    "dev/tests/kernel/test_llm_gateway.py",
    "dev/tests/kernel/test_model_config.py",
    "dev/tests/kernel/test_model_invoker.py",
    "dev/tests/kernel/test_model_policy.py",
    "dev/tests/kernel/test_output_delivery.py",
    "dev/tests/architecture/test_dependency_boundaries.py",
    "dev/tests/architecture/test_boundary_counterexamples.py",
    "dev/tests/architecture/test_plugin_lifecycle.py",
    "dev/tests/kernel/test_request_selector.py",
    "dev/tests/kernel/test_security_paths.py",
    "dev/tests/kernel/test_skill_engine.py",
    "dev/tests/tooling/test_export_safety.py",
    "dev/tests/kernel/test_task_spec.py",
    "dev/tests/kernel/test_task_spec_service.py",
    "dev/tests/tooling/test_verification_cli.py",
    "dev/tests/tooling/test_verification_failures.py",
]


def validate_safe_path(path: str, repo_root: str) -> str:
    """无条件计算 realpath，验证其严格归属于 repo_root 内部。
    阻断任意目录/文件软链接逃逸与同名前缀目录绕过（如 /repo-evil）。
    """
    real_repo = os.path.realpath(repo_root)
    real_target = os.path.realpath(path)

    try:
        common = os.path.commonpath([real_repo, real_target])
        if common != real_repo:
            raise ValueError(f"安全拒绝: 路径逃逸仓库根: {path} -> {real_target}")
    except ValueError:
        raise ValueError(f"安全拒绝: 路径逃逸仓库根: {path} -> {real_target}")

    if real_target != real_repo and not real_target.startswith(real_repo + os.sep):
        raise ValueError(f"安全拒绝: 路径逃逸仓库根: {path} -> {real_target}")

    return real_target


def create_sanitized_snapshots(agent_root: str, parser_root: str) -> Tuple[str, str, str]:
    """生成只包含明确源码、规则与已核实脱敏 fixtures 的临时代码快照，严格排除私人配置、未脱敏邮件与凭据。"""
    tmp_base = tempfile.mkdtemp(prefix="lite_agent_e0_sanitized_")
    try:
        agent_snap = os.path.join(tmp_base, "agent")
        parser_snap = os.path.join(tmp_base, "parser")
        os.makedirs(agent_snap, exist_ok=True)
        os.makedirs(parser_snap, exist_ok=True)

        # 1. 复制 Parser 源码、规则与脱敏 fixtures (显式白名单)
        for f in ALLOWED_PARSER_ROOT_FILES:
            src = os.path.join(parser_root, f)
            if os.path.isfile(src):
                validate_safe_path(src, parser_root)
                shutil.copy2(src, os.path.join(parser_snap, f))

        rules_src = os.path.join(parser_root, "rules")
        if os.path.isdir(rules_src):
            validate_safe_path(rules_src, parser_root)
            dst_rules = os.path.join(parser_snap, "rules")
            os.makedirs(dst_rules, exist_ok=True)
            for f in os.listdir(rules_src):
                if f.endswith(".json"):
                    src = os.path.join(rules_src, f)
                    validate_safe_path(src, parser_root)
                    shutil.copy2(src, os.path.join(dst_rules, f))

        tests_src = os.path.join(parser_root, "tests")
        if os.path.isdir(tests_src):
            validate_safe_path(tests_src, parser_root)
            dst_tests = os.path.join(parser_snap, "tests")
            os.makedirs(dst_tests, exist_ok=True)
            for f in os.listdir(tests_src):
                src = os.path.join(tests_src, f)
                if os.path.isfile(src) and f.endswith(".py"):
                    validate_safe_path(src, parser_root)
                    shutil.copy2(src, os.path.join(dst_tests, f))

            fixtures_src = os.path.join(tests_src, "fixtures")
            if os.path.isdir(fixtures_src):
                validate_safe_path(fixtures_src, parser_root)
                dst_fixtures = os.path.join(dst_tests, "fixtures")
                os.makedirs(dst_fixtures, exist_ok=True)
                for f in ALLOWED_PARSER_FIXTURES:
                    src = os.path.join(fixtures_src, f)
                    if os.path.isfile(src):
                        validate_safe_path(src, parser_root)
                        shutil.copy2(src, os.path.join(dst_fixtures, f))

        scripts_src = os.path.join(parser_root, "scripts")
        if os.path.isdir(scripts_src):
            validate_safe_path(scripts_src, parser_root)
            dst_scripts = os.path.join(parser_snap, "scripts")
            os.makedirs(dst_scripts, exist_ok=True)
            for f in os.listdir(scripts_src):
                src = os.path.join(scripts_src, f)
                if os.path.isfile(src) and f.endswith(".py"):
                    validate_safe_path(src, parser_root)
                    shutil.copy2(src, os.path.join(dst_scripts, f))

        # 2. 复制 Agent 源码与脱敏配置 (显式白名单：仅允许确定的 .py、受控静态资源与脱敏 fixture)
        for d in ALLOWED_AGENT_CODE_DIRS:
            src_d = os.path.join(agent_root, d)
            if not os.path.isdir(src_d):
                continue
            validate_safe_path(src_d, agent_root)
            dst_d = os.path.join(agent_snap, d)
            for root, dirs, files in os.walk(src_d):
                validate_safe_path(root, agent_root)
                dirs[:] = [
                    sub for sub in dirs
                    if sub not in ('.git', '__pycache__', '.pytest_cache', 'data', 'scratch', 'deploy', 'validation-reports', 'email-downloads')
                ]
                rel_dir = os.path.relpath(root, src_d)
                target_dir = os.path.join(dst_d, rel_dir) if rel_dir != '.' else dst_d

                for f in files:
                    src_file = os.path.join(root, f)
                    validate_safe_path(src_file, agent_root)
                    rel_from_agent = os.path.relpath(src_file, agent_root)

                    # 排除数据库文件、锁文件、临时文件与所有 sidecar 扩展名 (无论命名)
                    if any(f.endswith(ext) for ext in ('.db', '.sqlite', '.sqlite3', '.db-wal', '.db-shm', '.sqlite-wal', '.sqlite-shm', '.sqlite3-wal', '.sqlite3-shm', '.lock', '.tmp')):
                        continue
                    # 排除所有 .env 文件
                    if f.startswith('.env') or f.endswith('.env'):
                        continue
                    # 排除子目录中任何 private config / 凭据文件
                    if f == 'config.json' or f.endswith('.pem') or f.endswith('.key'):
                        continue

                    should_copy = False
                    if f.endswith('.py'):
                        should_copy = True
                    elif rel_from_agent in ALLOWED_AGENT_EXACT_FILES:
                        should_copy = True
                    elif d == 'conf.d' and f.endswith('.json.example'):
                        should_copy = True
                    elif d == 'web_dashboard' and any(f.endswith(ext) for ext in ('.html', '.css', '.js')):
                        should_copy = True

                    if should_copy:
                        os.makedirs(target_dir, exist_ok=True)
                        shutil.copy2(src_file, os.path.join(target_dir, f))

        for f in ALLOWED_AGENT_ROOT_FILES:
            src_f = os.path.join(agent_root, f)
            if os.path.isfile(src_f):
                validate_safe_path(src_f, agent_root)
                target = os.path.join(agent_snap, f)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(src_f, target)

        # 生成脱敏 config.json（由 config.example.json 生成）
        example_cfg = os.path.join(agent_snap, 'dev/config/examples/agent.json')
        if not os.path.isfile(example_cfg):
            example_cfg = os.path.join(agent_snap, 'config/examples/agent.json')
        if os.path.isfile(example_cfg):
            shutil.copy2(example_cfg, os.path.join(agent_snap, 'config.json'))

        return tmp_base, agent_snap, parser_snap
    except Exception:
        shutil.rmtree(tmp_base, ignore_errors=True)
        raise


def ensure_docker_image(agent_root: str, force_rebuild: bool = False) -> bool:
    """确认 lite_agent_test:3.9 镜像存在且包含离线 wheel 缓存，不满足则使用独立隔离构建上下文目录构建。"""
    if not force_rebuild:
        check_cmd = ["docker", "run", "--rm", "lite_agent_test:3.9", "test", "-d", "/wheels"]
        res = subprocess.run(check_cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return True
    return build_isolated_docker_image(agent_root, "lite_agent_test:3.9")


def build_isolated_docker_image(agent_root: str, tag: str = "lite_agent_test:3.9") -> bool:
    """在独立临时目录构建测试镜像，绝对不向 Docker builder 传递仓库其它文件。"""
    dockerfile_src = os.path.join(agent_root, "dev/config/docker/Dockerfile.test")
    if not os.path.isfile(dockerfile_src):
        dockerfile_src = os.path.join(agent_root, "config/docker/Dockerfile.test")
    req_src = os.path.join(agent_root, "dev/requirements/test.txt")
    if not os.path.isfile(req_src):
        req_src = os.path.join(agent_root, "requirements/test.txt")
    if not os.path.isfile(dockerfile_src) or not os.path.isfile(req_src):
        print(f"❌ 缺少测试镜像构建文件: {dockerfile_src} 或 {req_src}")
        return False

    build_ctx = tempfile.mkdtemp(prefix="lite_agent_docker_ctx_")
    try:
        shutil.copy2(dockerfile_src, os.path.join(build_ctx, "Dockerfile.test"))
        shutil.copy2(req_src, os.path.join(build_ctx, "requirements-test.txt"))
        print(f"🔨 正在基于专用隔离上下文目录 ({build_ctx}) 构建测试镜像 {tag}...")
        build_cmd = [
            "docker", "build", "-t", tag,
            "-f", os.path.join(build_ctx, "Dockerfile.test"),
            build_ctx
        ]
        build_res = subprocess.run(build_cmd, capture_output=True, text=True)
        if build_res.returncode != 0:
            print("❌ Docker build 失败:\n" + build_res.stderr[-800:])
            return False
        return True
    finally:
        shutil.rmtree(build_ctx, ignore_errors=True)


def extract_test_counts(output: str) -> str:
    """从 pytest 或 unittest 输出中提取实际运行用例数。"""
    # 匹配 pytest 汇总行: e.g., "724 passed in 56.40s"
    m_pytest = re.search(r"(\d+\s+passed(?:\s*,\s*\d+\s+subtests?\s+passed)?)", output)
    if m_pytest:
        return m_pytest.group(1)
    # 匹配 unittest 汇总行: e.g., "Ran 96 tests in 1.437s"
    m_unittest = re.search(r"Ran\s+(\d+)\s+tests", output)
    if m_unittest:
        return f"{m_unittest.group(1)} tests passed"
    return ""


def run_step(name: str, cmd: list, cwd: str, timeout: int = 180) -> Dict[str, Any]:
    start_time = time.time()
    print(f"\n==================================================")
    print(f"▶ 正在执行: {name}")
    print(f"  命令: {' '.join(cmd)}")
    print(f"  工作目录: {cwd}")
    print(f"==================================================")

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        elapsed = time.time() - start_time
        passed = (proc.returncode == 0)

        stdout_snippet = proc.stdout.strip()
        stderr_snippet = proc.stderr.strip()
        combined = stdout_snippet + "\n" + stderr_snippet
        fd, raw_log_path = tempfile.mkstemp(prefix="kernel-gate-output-", suffix=".log")
        with os.fdopen(fd, "w", encoding="utf-8") as raw_log:
            raw_log.write("COMMAND: " + repr(cmd) + "\n")
            raw_log.write(f"EXIT: {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        print(f"  完整原始输出: {raw_log_path}")
        test_metric = extract_test_counts(combined)

        if passed:
            metric_str = f" ({test_metric})" if test_metric else ""
            print(f"✅ {name} 通过{metric_str} (耗时 {elapsed:.2f}s)")
            if stdout_snippet:
                lines = stdout_snippet.splitlines()
                for l in lines[-6:]:
                    print(f"   {l}")
        else:
            print(f"❌ {name} 失败 (退出码 {proc.returncode}, 耗时 {elapsed:.2f}s)")
            if stdout_snippet:
                print("--- stdout ---")
                print(stdout_snippet[-1200:])
            if stderr_snippet:
                print("--- stderr ---")
                print(stderr_snippet[-1200:])

        return {
            "name": name,
            "passed": passed,
            "exit_code": proc.returncode,
            "elapsed": elapsed,
            "metric": test_metric,
            "error_tail": stderr_snippet[-500:] if stderr_snippet else "",
            "raw_log": raw_log_path,
        }
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        print(f"❌ {name} 超时 ({timeout}s)")
        return {
            "name": name,
            "passed": False,
            "exit_code": -1,
            "elapsed": elapsed,
            "metric": "",
            "error_tail": "执行超时",
        }
    except Exception as exc:
        elapsed = time.time() - start_time
        print(f"❌ {name} 异常: {exc}")
        return {
            "name": name,
            "passed": False,
            "exit_code": -1,
            "elapsed": elapsed,
            "metric": "",
            "error_tail": str(exc),
        }


def check_formatting(repo_root: str) -> Tuple[bool, str]:
    """检查 tracked working diff、staged diff 以及 untracked 新文件的格式与末尾空白。"""
    if not shutil.which("git"):
        return False, "环境未安装 git，无法完成格式与 diff 检查"

    # 1. 检查 unstaged diff
    r1 = subprocess.run(["git", "diff", "--check"], cwd=repo_root, capture_output=True, text=True)
    if r1.returncode != 0:
        return False, (r1.stdout + "\n" + r1.stderr).strip()

    # 2. 检查 staged diff
    r2 = subprocess.run(["git", "diff", "--cached", "--check"], cwd=repo_root, capture_output=True, text=True)
    if r2.returncode != 0:
        return False, (r2.stdout + "\n" + r2.stderr).strip()

    # 3. 检查新增未跟踪文件 (untracked files) 的末尾空白和换行
    r3 = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo_root, capture_output=True, text=True)
    if r3.returncode != 0:
        return False, (r3.stdout + "\n" + r3.stderr).strip()
    if r3.returncode == 0:
        errs = []
        for rel_path in r3.stdout.splitlines():
            abs_path = os.path.join(repo_root, rel_path)
            if not os.path.isfile(abs_path):
                continue
            if any(rel_path.endswith(ext) for ext in ('.png', '.jpg', '.tar.gz', '.zip', '.pyc', '.sqlite', '.db')):
                continue
            try:
                with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line_no, line in enumerate(f, 1):
                        stripped_nl = line.rstrip('\r\n')
                        if stripped_nl != stripped_nl.rstrip():
                            errs.append(f"{rel_path}:{line_no}: 存在末尾空白字符 (trailing whitespace)")
            except Exception as e:
                errs.append(f"{rel_path}: 读取失败 {e}")
        if errs:
            return False, "\n".join(errs[:15])

    return True, ""


def run_format_step(name: str, repo_root: str) -> Dict[str, Any]:
    start_time = time.time()
    print(f"\n==================================================")
    print(f"▶ 正在执行: {name}")
    print(f"  工作目录: {repo_root}")
    print(f"==================================================")
    passed, msg = check_formatting(repo_root)
    elapsed = time.time() - start_time
    if passed:
        print(f"✅ {name} 通过 (耗时 {elapsed:.2f}s)")
        return {
            "name": name,
            "passed": True,
            "exit_code": 0,
            "elapsed": elapsed,
            "metric": "git & whitespace clean",
            "error_tail": "",
        }
    else:
        print(f"❌ {name} 失败 (耗时 {elapsed:.2f}s):\n{msg}")
        return {
            "name": name,
            "passed": False,
            "exit_code": 1,
            "elapsed": elapsed,
            "metric": "format error",
            "error_tail": msg[-500:],
        }


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lite Agent 全量门禁与隔离验证入口 (scripts/verify.py)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--agent-root",
        type=str,
        default=None,
        help="Agent 仓库根路径 (默认根据脚本位置自动推导)",
    )
    parser.add_argument(
        "--parser-root",
        type=str,
        default=None,
        help="Parser 仓库根路径 (默认兼容 ../dev/mail-statement-parser)",
    )
    parser.add_argument(
        "--core-only",
        action="store_true",
        help="纯基础 core 门禁模式 (无需 Parser 仓库，执行 AST 门禁、core 测试、前端及隔离验证)",
    )
    parser.add_argument(
        "--skip-docker",
        action="store_true",
        help="跳过 Docker Python 3.9 容器验证",
    )
    parser.add_argument(
        "--skip-parser",
        action="store_true",
        help="跳过 Parser 跨仓测试 (明确声明缺权限或纯内核构建)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    agent_root = os.path.abspath(args.agent_root) if args.agent_root else os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    )
    if not os.path.isdir(agent_root):
        print(f"❌ 关键错误: 找不到 Agent 根目录: {agent_root}", file=sys.stderr)
        sys.exit(2)

    default_parser = os.path.abspath(os.path.join(agent_root, "..", "dev", "mail-statement-parser"))
    parser_root = os.path.abspath(args.parser_root) if args.parser_root else default_parser

    need_parser = not args.core_only and not args.skip_parser
    if need_parser and not os.path.isdir(parser_root):
        print(f"❌ 关键错误: 找不到 parser 兄弟仓库: {parser_root}", file=sys.stderr)
        print("💡 若需独立验证无需 Parser 的基础内核门禁，请使用 --core-only 或 --skip-parser", file=sys.stderr)
        sys.exit(2)
    if need_parser and os.path.isdir(parser_root):
        os.environ["MAIL_STATEMENT_PARSER_DIR"] = parser_root

    print("==================================================")
    print("🚀 Lite Agent 全量门禁与隔离验证启动")
    print(f"  模式:        {'CORE 独立基础门禁' if args.core_only else 'FULL 全量门禁 (含跨仓 Parser)'}")
    print(f"  Agent 目录:  {agent_root}")
    if need_parser:
        print(f"  Parser 目录: {parser_root}")
    print(f"  当前 Python: {sys.executable} ({sys.version.split()[0]})")
    print("==================================================")

    results = []

    # 1. 本机架构静态依赖门禁 (AST check_architecture)
    results.append(run_step(
        "1. 静态架构门禁 (AST check_architecture)",
        [sys.executable, os.path.join(agent_root, "scripts", "check_architecture.py")],
        cwd=agent_root,
    ))

    # 2. S3-A1 核心基础源码清单与导出安全检查
    manifest_test = os.path.join(agent_root, "dev", "tests", "tooling", "test_core_manifest_and_export.py")
    if not os.path.isfile(manifest_test):
        manifest_test = os.path.join(agent_root, "tests", "tooling", "test_core_manifest_and_export.py")
    if os.path.isfile(manifest_test):
        results.append(run_step(
            "2. 基础源码清单与导出安全防御 (S3-A1 unittest)",
            [sys.executable, "-m", "unittest", manifest_test, "-q"],
            cwd=agent_root,
        ))
    else:
        print(f"❌ 关键错误: 缺少必要门禁测试文件: {manifest_test}", file=sys.stderr)
        results.append({
            "name": "2. 基础源码清单与导出安全防御 (S3-A1 unittest)",
            "passed": False,
            "exit_code": 1,
            "elapsed": 0,
            "metric": "",
            "error_tail": f"缺少文件: {manifest_test}",
        })

    # 3. 前端独立 JavaScript 契约检查 (若存在 Node 环境)
    frontend_test = os.path.join(agent_root, "dev", "tests", "frontend", "test_review_meeting_frontend.js")
    if not os.path.isfile(frontend_test):
        frontend_test = os.path.join(agent_root, "tests", "frontend", "test_review_meeting_frontend.js")
    if not os.path.isfile(frontend_test):
        print(f"❌ 关键错误: 缺少必要前端测试文件: {frontend_test}", file=sys.stderr)
        results.append({
            "name": "3. 前端独立 JavaScript 契约 (Node.js)",
            "passed": False,
            "exit_code": 1,
            "elapsed": 0,
            "metric": "",
            "error_tail": f"缺少文件: {frontend_test}",
        })
    else:
        node_bin = shutil.which("node")
        if node_bin:
            results.append(run_step(
                "3. 前端独立 JavaScript 契约 (Node.js)",
                [node_bin, frontend_test],
                cwd=agent_root,
            ))
        else:
            print("⚠️ 未检测到 Node.js，前端检查标记为 PENDING")
            results.append({
                "name": "3. 前端独立 JavaScript 契约 (Node.js)",
                "passed": False,
                "exit_code": 1,
                "elapsed": 0,
                "metric": "PENDING (Node not installed)",
                "error_tail": "Node not installed",
            })

    # 4. Agent 回归测试套件 (严格使用当前解释器 sys.executable，缺依赖报错，禁止隐式回退)
    try:
        import pytest  # noqa: F401
        pytest_cmd = [sys.executable, "-m", "pytest"]
    except ImportError:
        print(f"❌ 关键错误: 当前解释器 ({sys.executable}) 缺少 pytest 依赖，拒绝隐式回退外部解释器", file=sys.stderr)
        pytest_cmd = None

    if pytest_cmd is None:
        results.append({
            "name": f"4. Agent {'基础核心' if args.core_only else '全量业务'}回归 (pytest)",
            "passed": False,
            "exit_code": 1,
            "elapsed": 0,
            "metric": "",
            "error_tail": f"当前解释器 {sys.executable} 未安装 pytest",
        })
    else:
        test_dir = "dev/tests/" if os.path.isdir(os.path.join(agent_root, "dev/tests")) else "tests/"
        pytest_ini = "dev/config/pytest.ini" if os.path.isfile(os.path.join(agent_root, "dev/config/pytest.ini")) else "pytest.ini"
        no_skips_plugin = "dev.tests.support.no_skips" if test_dir == "dev/tests/" else "tests.support.no_skips"

        if args.core_only:
            test_target_args = list(CORE_TEST_FILES)
            step_title = "4. Agent 基础核心回归与边界测试 (pytest --core-only)"
        else:
            test_target_args = [test_dir]
            step_title = "4. Agent 全量回归与副作用隔离 (pytest)"

        results.append(run_step(
            step_title,
            pytest_cmd + ["-c", pytest_ini, "--rootdir=."] + test_target_args + ["-q", "-p", "no:cacheprovider", "-p", no_skips_plugin, "--tb=short"],
            cwd=agent_root,
            timeout=600,
        ))

    # 5. Parser 全量测试套件 (unittest，仅在非 core-only 模式下执行)
    if need_parser:
        results.append(run_step(
            "5. Parser 路径隔离与全量回归 (unittest)",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
            cwd=parser_root,
            timeout=60,
        ))

    # 6. Git Diff 与新增文件格式规范检查
    results.append(run_format_step("6. Agent Git 格式与末尾空白规范", agent_root))
    if need_parser:
        results.append(run_format_step("7. Parser Git 格式与末尾空白规范", parser_root))

    # 7. Docker Python 3.9 隔离环境验证
    has_docker = shutil.which("docker") is not None and not args.skip_docker
    if has_docker:
        print("\n📦 正在构建并验证代码脱敏快照...")
        tmp_base, agent_snap, parser_snap = create_sanitized_snapshots(agent_root, parser_root if need_parser else agent_root)
        try:
            if need_parser:
                # Docker Python 3.9: Parser 全量测试 (代码只读挂载 :ro，禁外网)
                results.append(run_step(
                    "8. Docker Python 3.9: Parser 全量测试 (代码只读 :ro, 无外网)",
                    [
                        "docker", "run", "--rm", "--network=none",
                        "-v", f"{parser_snap}:/parser:ro",
                        "-e", "MAIL_STATEMENT_DATA_ROOT=/tmp/data_root",
                        "-e", "PYTHONDONTWRITEBYTECODE=1",
                        "-w", "/parser",
                        "python:3.9-slim",
                        "python", "-m", "unittest", "discover", "-s", "tests", "-q"
                    ],
                    cwd=agent_root,
                    timeout=300,
                ))

            # Docker Python 3.9: AST 架构门禁 (代码只读挂载 :ro, 禁外网)
            results.append(run_step(
                f"{'9' if need_parser else '7'}. Docker Python 3.9: AST 架构依赖门禁 (代码只读 :ro, 无外网)",
                [
                    "docker", "run", "--rm", "--network=none",
                    "-v", f"{agent_snap}:/app:ro",
                    "-e", "PYTHONDONTWRITEBYTECODE=1",
                    "-w", "/app",
                    "python:3.9-slim",
                    "python", "scripts/check_architecture.py"
                ],
                cwd=agent_root,
                timeout=60,
            ))

            # Docker Python 3.9: Agent 回归 pytest (按模式精确选择 core / full，代码只读挂载 :ro, 禁外网)
            has_agent_image = ensure_docker_image(agent_root)
            if has_agent_image:
                agent_test_cmd = [
                    "docker", "run", "--rm", "--network=none",
                    "-v", f"{agent_snap}:/app:ro",
                ]
                if need_parser:
                    agent_test_cmd.extend(["-v", f"{parser_snap}:/parser:ro", "-e", "MAIL_STATEMENT_PARSER_DIR=/parser"])
                docker_test_dir = "dev/tests/" if os.path.isdir(os.path.join(agent_snap, "dev/tests")) else "tests/"
                docker_pytest_ini = "dev/config/pytest.ini" if os.path.isfile(os.path.join(agent_snap, "dev/config/pytest.ini")) else "pytest.ini"
                docker_no_skips = "dev.tests.support.no_skips" if docker_test_dir == "dev/tests/" else "tests.support.no_skips"
                target_pytest_args = list(CORE_TEST_FILES) if args.core_only else [docker_test_dir]
                agent_test_cmd.extend([
                    "-e", "PYTHONDONTWRITEBYTECODE=1",
                    "-w", "/app",
                    "lite_agent_test:3.9",
                    "python", "-m", "pytest",
                    "-c", docker_pytest_ini, "--rootdir=.",
                ] + target_pytest_args + [
                    "-q", "-p", "no:cacheprovider", "-p", docker_no_skips, "-o", "cache_dir=/tmp/.pytest_cache", "--tb=short"
                ])
                results.append(run_step(
                    f"{'10' if need_parser else '8'}. Docker Python 3.9: Agent {'核心' if args.core_only else '全量'}回归测试 (代码只读 :ro, 无外网)",
                    agent_test_cmd,
                    cwd=agent_root,
                    timeout=600,
                ))
            else:
                results.append({
                    "name": f"{'10' if need_parser else '8'}. Docker Python 3.9: Agent 回归测试",
                    "passed": False,
                    "exit_code": 1,
                    "elapsed": 0,
                    "metric": "PENDING (Image build failed)",
                    "error_tail": "Image build failed",
                })
        finally:
            shutil.rmtree(tmp_base, ignore_errors=True)
    elif args.skip_docker:
        print("\n⏩ 显式指定 --skip-docker，跳过容器验证")
    else:
        print("\n⚠️ 未检测到 Docker 环境，Python 3.9 容器验证项标记为 PENDING")
        results.append({
            "name": "Docker Python 3.9 容器验证",
            "passed": False,
            "exit_code": 1,
            "elapsed": 0,
            "metric": "PENDING (Docker not installed)",
            "error_tail": "Docker not installed",
        })

    # 汇总输出
    print("\n==================================================")
    print("📊 门禁验证汇总报告")
    print("==================================================")
    all_passed = True
    for r in results:
        status_icon = "✅ PASS" if r["passed"] else "❌ FAIL"
        metric_disp = f" [{r['metric']}]" if r.get("metric") else ""
        print(f"{status_icon} | {r['name']:<55} | 耗时 {r['elapsed']:>6.2f}s{metric_disp}")
        if not r["passed"]:
            all_passed = False

    skipped_items = []
    if args.core_only:
        skipped_items.append("全量业务与跨仓集成 (模式: --core-only)")
    if args.skip_parser:
        skipped_items.append("Parser 跨仓测试 (--skip-parser)")
    if args.skip_docker:
        skipped_items.append("Docker 容器双回归 (--skip-docker)")

    print("==================================================")
    if all_passed:
        if skipped_items:
            print("⚠️ 缩减验证通过！本次运行包含以下跳过项，非完整双环境全量验收：")
            for item in skipped_items:
                print(f"   - {item}")
        else:
            print("🎉 恭喜！所有硬性门禁（含本机与 Docker Python 3.9 全量双回归）全部通过！")
        sys.exit(0)
    else:
        print("⚠️ 存在未通过门禁项，请排查上方失败信息。")
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
E0 阶段可复现验证门禁入口 (scripts/verify_e0.py)
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
    'config.example.json', 'architecture_rules.json', 'requirements-test.txt',
    'pytest.ini', 'Dockerfile.test'
}

ALLOWED_AGENT_EXACT_FILES = {
    'edge_node/whitelist.json',
    'scripts/whitelist.json',
    'scripts/rss_topic/paths.json',
    'tests/wechat_ilink/fixtures/getupdates_success.json',
    'tests/wechat_ilink/fixtures/sendmessage_success_octet_stream.json',
    'tests/test_review_meeting_frontend.js',
}

ALLOWED_AGENT_CODE_DIRS = [
    'app', 'architecture', 'channels', 'contracts', 'core', 'edge_node', 'features',
    'integrations', 'memory_engine', 'services', 'skills', 'scripts',
    'tests', 'web_dashboard', 'conf.d'
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
                    if sub not in ('.git', '__pycache__', 'data', 'scratch', 'deploy', 'validation-reports', 'email-downloads')
                ]
                rel_dir = os.path.relpath(root, src_d)
                target_dir = os.path.join(dst_d, rel_dir) if rel_dir != '.' else dst_d

                for f in files:
                    src_file = os.path.join(root, f)
                    validate_safe_path(src_file, agent_root)
                    rel_from_agent = os.path.relpath(src_file, agent_root)

                    # 排除数据库文件与所有 sidecar 扩展名 (无论命名)
                    if any(f.endswith(ext) for ext in ('.db', '.sqlite', '.sqlite3', '.db-wal', '.db-shm', '.sqlite-wal', '.sqlite-shm', '.sqlite3-wal', '.sqlite3-shm')):
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
                shutil.copy2(src_f, os.path.join(agent_snap, f))

        # 生成脱敏 config.json（由 config.example.json 生成）
        example_cfg = os.path.join(agent_snap, 'config.example.json')
        if os.path.isfile(example_cfg):
            shutil.copy2(example_cfg, os.path.join(agent_snap, 'config.json'))

        return tmp_base, agent_snap, parser_snap
    except Exception:
        shutil.rmtree(tmp_base, ignore_errors=True)
        raise


def ensure_docker_image(agent_root: str, force_rebuild: bool = False) -> bool:
    """确认 lite_agent_test:3.9 镜像存在，不存在则使用独立隔离构建上下文目录构建。"""
    if not force_rebuild:
        check_cmd = ["docker", "image", "inspect", "lite_agent_test:3.9"]
        res = subprocess.run(check_cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return True
    return build_isolated_docker_image(agent_root, "lite_agent_test:3.9")


def build_isolated_docker_image(agent_root: str, tag: str = "lite_agent_test:3.9") -> bool:
    """在独立临时目录构建测试镜像，绝对不向 Docker builder 传递仓库其它文件。"""
    dockerfile_src = os.path.join(agent_root, "Dockerfile.test")
    req_src = os.path.join(agent_root, "requirements-test.txt")
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


def main():
    agent_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    parser_root = os.path.abspath(os.path.join(agent_root, "..", "dev", "mail-statement-parser"))

    print("==================================================")
    print("🚀 Lite Agent E0 阶段全量门禁与隔离验证启动")
    print(f"  Agent 目录:  {agent_root}")
    print(f"  Parser 目录: {parser_root}")
    print(f"  当前 Python: {sys.executable} ({sys.version.split()[0]})")
    print("==================================================")

    if not os.path.isdir(parser_root):
        print(f"❌ 关键错误: 找不到 parser 兄弟仓库: {parser_root}")
        sys.exit(2)

    results = []

    # 1. 本机架构静态依赖门禁
    results.append(run_step(
        "1. 静态架构门禁 (AST check_architecture)",
        [sys.executable, os.path.join(agent_root, "scripts", "check_architecture.py")],
        cwd=agent_root,
    ))

    # 2. Agent 全量测试套件 (动态检测 pytest 可执行入口)
    pytest_cmd = None
    try:
        import pytest  # noqa: F401
        pytest_cmd = [sys.executable, "-m", "pytest"]
    except ImportError:
        pytest_bin = shutil.which("pytest")
        if pytest_bin:
            pytest_cmd = [pytest_bin]
        else:
            py313 = shutil.which("python3.13") or "/opt/homebrew/opt/python@3.13/bin/python3.13"
            if os.path.isfile(py313):
                pytest_cmd = [py313, "-m", "pytest"]
            else:
                pytest_cmd = [sys.executable, "-m", "pytest"]

    results.append(run_step(
        "2. Agent 全量回归与副作用隔离 (pytest)",
        pytest_cmd + ["-q", "-p", "no:cacheprovider", "--tb=short"],
        cwd=agent_root,
        timeout=180,
    ))

    # 3. Parser 全量测试套件 (unittest)
    results.append(run_step(
        "3. Parser 路径隔离与全量回归 (unittest)",
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        cwd=parser_root,
        timeout=60,
    ))

    # 4. Git Diff 格式检查
    results.append(run_step(
        "4. Agent Git Diff 格式规范",
        ["git", "diff", "--check"],
        cwd=agent_root,
    ))
    results.append(run_step(
        "5. Parser Git Diff 格式规范",
        ["git", "diff", "--check"],
        cwd=parser_root,
    ))

    # 5. Docker Python 3.9 隔离环境验证
    has_docker = shutil.which("docker") is not None
    if has_docker:
        print("\n📦 正在构建并验证代码脱敏快照...")
        tmp_base, agent_snap, parser_snap = create_sanitized_snapshots(agent_root, parser_root)
        try:
            # Step 6: Docker Python 3.9: Parser 全量测试 (代码只读挂载 :ro，禁外网)
            results.append(run_step(
                "6. Docker Python 3.9: Parser 全量测试 (代码只读 :ro, 无外网)",
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
                timeout=120,
            ))

            # Step 7: Docker Python 3.9: AST 架构门禁 (代码只读挂载 :ro, 禁外网)
            results.append(run_step(
                "7. Docker Python 3.9: AST 架构依赖门禁 (代码只读 :ro, 无外网)",
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

            # Step 8: Docker Python 3.9: Agent 全量 pytest (代码只读挂载 :ro, 禁外网)
            has_agent_image = ensure_docker_image(agent_root)
            if has_agent_image:
                results.append(run_step(
                    "8. Docker Python 3.9: Agent 全量回归测试 (代码只读 :ro, 无外网)",
                    [
                        "docker", "run", "--rm", "--network=none",
                        "-v", f"{agent_snap}:/app:ro",
                        "-v", f"{parser_snap}:/parser:ro",
                        "-e", "PYTHONDONTWRITEBYTECODE=1",
                        "-e", "MAIL_STATEMENT_PARSER_DIR=/parser",
                        "-w", "/app",
                        "lite_agent_test:3.9",
                        "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "cache_dir=/tmp/.pytest_cache", "--tb=short"
                    ],
                    cwd=agent_root,
                    timeout=180,
                ))
            else:
                results.append({
                    "name": "8. Docker Python 3.9: Agent 全量回归测试 (代码只读 :ro, 无外网)",
                    "passed": False,
                    "exit_code": 1,
                    "elapsed": 0,
                    "metric": "PENDING (Image build failed)",
                    "error_tail": "Image build failed",
                })
        finally:
            shutil.rmtree(tmp_base, ignore_errors=True)
    else:
        print("\n⚠️ 未检测到 Docker 环境，Python 3.9 容器验证项标记为 PENDING")
        results.append({
            "name": "6-8. Docker Python 3.9 容器验证",
            "passed": False,
            "exit_code": 1,
            "elapsed": 0,
            "metric": "PENDING (Docker not installed)",
            "error_tail": "Docker not installed",
        })

    # 汇总输出
    print("\n==================================================")
    print("📊 E0 阶段全量门禁验证汇总报告")
    print("==================================================")
    all_passed = True
    for r in results:
        status_icon = "✅ PASS" if r["passed"] else "❌ FAIL"
        metric_disp = f" [{r['metric']}]" if r.get("metric") else ""
        print(f"{status_icon} | {r['name']:<50} | 耗时 {r['elapsed']:>6.2f}s{metric_disp}")
        if not r["passed"]:
            all_passed = False

    print("==================================================")
    if all_passed:
        print("🎉 恭喜！E0 阶段所有 8 项硬性门禁（含本机与 Docker Python 3.9 全量双回归）全部通过！")
        sys.exit(0)
    else:
        print("⚠️ 存在未通过门禁项，请排查上方失败信息。")
        sys.exit(1)


if __name__ == "__main__":
    main()

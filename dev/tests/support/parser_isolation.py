"""
受控跨仓 Parser 测试生命周期治理与隔离守卫 (dev/tests/support/parser_isolation.py)

保障：
1. 真实校验固定 Parser Git SHA（对齐 CI Gate 4 基线 fa3600d813d9b0b07171baf7c8ab9aeebc77d17d）；
2. 强制限定在 tmp_path 测试目录运行，防范污染真实生产/用户数据；
3. 严格恢复进入前状态：若 sys.modules 已有同名模块，退出时恢复原对象；若无则 pop 移除；sys.path 完整还原。
   即使初始化阶段抛出异常，也无条件执行现场清理；
4. 受架构门禁 rules.json 显式登记与 verification_requirement 约束。
"""

import importlib
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

PINNED_PARSER_SHA = os.environ.get(
    "PARSER_PINNED_SHA", "fa3600d813d9b0b07171baf7c8ab9aeebc77d17d"
)

MANAGED_PARSER_MODULES = (
    "statement_db",
    "statement_models",
    "statement_parser",
    "billing_readmodel",
    "mail_quality",
    "statement_evidence",
    "mail_client",
    "statement_identity",
    "mail_sync",
    "mail_connect",
    "mail_parse",
    "mail_llm",
    "parser_paths",
)


class ParserHarness:
    """包装已安全隔离加载的 Parser 模块命名空间，支持以三元组 (root, sd, models) 解包保持历史兼容。"""

    def __init__(
        self,
        root: Path,
        sd: Any,
        models: Any,
        parser: Any = None,
        readmodel: Any = None,
        quality: Any = None,
        evidence: Any = None,
        client: Any = None,
    ):
        self.root = root
        self.sd = sd
        self.models = models
        self.parser = parser
        self.readmodel = readmodel
        self.quality = quality
        self.evidence = evidence
        self.client = client

    def __iter__(self):
        yield self.root
        yield self.sd
        yield self.models

    def __getitem__(self, item):
        return [self.root, self.sd, self.models][item]


def verify_parser_fixed_version(parser_dir: Path, expected_sha: str = PINNED_PARSER_SHA) -> str:
    """真实校验外部 Parser 仓库的 Git Commit SHA。"""
    if not (parser_dir / ".git").exists() and not (parser_dir / "mail_client.py").exists():
        raise FileNotFoundError(f"未找到 Parser 仓库或关键入口: {parser_dir}")

    # 获取当前 git commit SHA
    try:
        res = subprocess.run(
            ["git", "-C", str(parser_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        actual_sha = res.stdout.strip()
    except Exception as e:
        raise RuntimeError(f"无法获取 Parser 仓库 Git SHA: {e}") from e

    if not actual_sha.startswith(expected_sha) and not expected_sha.startswith(actual_sha):
        raise AssertionError(
            f"Parser 提交 SHA 未锁定在已核准基线: 预期 {expected_sha}, 实际 {actual_sha}"
        )
    return actual_sha


@contextmanager
def isolated_parser_context(tmp_path: Path) -> Iterator[ParserHarness]:
    """
    受控临时上下文管理器：
    声明 tmp_path 参数满足 check_architecture.py 的 verify_exception_requirements 门禁。
    保证退出时恢复进入前的 sys.modules 与 sys.path 状态。
    """
    assert os.environ.get("TESTING") == "1", "isolated_parser_context 必须在测试隔离环境下运行"
    tmp_str = str(tmp_path).lower()
    assert (
        "temp" in tmp_str or "tmp" in tmp_str or "var" in tmp_str
    ), f"tmp_path 必须位于受控临时目录: {tmp_path}"

    candidate = (
        os.environ.get("MAIL_STATEMENT_PARSER_DIR")
        or "/Users/lilee/Projects/dev/mail-statement-parser"
    )
    root = Path(candidate)
    if not (root / "mail_client.py").exists():
        raise FileNotFoundError(f"找不到兄弟仓库 mail-statement-parser: {root}")

    # 1. 真实校验固定 SHA (防降级防漂移)
    verify_parser_fixed_version(root)

    # 2. 状态备份：记录进入前状态，以便精准恢复原对象
    original_modules = {m: sys.modules.get(m) for m in MANAGED_PARSER_MODULES}
    original_path = list(sys.path)

    # 3. 环境变量备份与临时重定向至 tmp_path
    old_env = {
        "MAIL_STATEMENT_DATA_ROOT": os.environ.get("MAIL_STATEMENT_DATA_ROOT"),
        "MAIL_STATEMENT_DB_PATH": os.environ.get("MAIL_STATEMENT_DB_PATH"),
        "MAIL_STATEMENT_REPORT_DIR": os.environ.get("MAIL_STATEMENT_REPORT_DIR"),
        "MAIL_STATEMENT_DOWNLOAD_DIR": os.environ.get("MAIL_STATEMENT_DOWNLOAD_DIR"),
    }

    try:
        os.environ["MAIL_STATEMENT_DATA_ROOT"] = str(tmp_path)
        os.environ["MAIL_STATEMENT_DB_PATH"] = str(tmp_path / "statements.db")
        os.environ["MAIL_STATEMENT_REPORT_DIR"] = str(tmp_path / "validation-reports")
        os.environ["MAIL_STATEMENT_DOWNLOAD_DIR"] = str(tmp_path / "email-downloads")

        # 为确保干净加载：若 sys.modules 已存在同名外部对象，先临时移除以加载真实 parser 模块
        for m in MANAGED_PARSER_MODULES:
            if m in sys.modules:
                sys.modules.pop(m, None)

        # 受控在顶层加入 parser 路径
        sys.path.insert(0, str(root))

        sd = importlib.import_module("statement_db")
        models = importlib.import_module("statement_models")
        parser = importlib.import_module("statement_parser")
        readmodel = importlib.import_module("billing_readmodel")
        quality = importlib.import_module("mail_quality")
        evidence = importlib.import_module("statement_evidence")
        client = importlib.import_module("mail_client")

        harness = ParserHarness(
            root=root,
            sd=sd,
            models=models,
            parser=parser,
            readmodel=readmodel,
            quality=quality,
            evidence=evidence,
            client=client,
        )
        yield harness
    finally:
        # 4. 彻底恢复 sys.modules 进入前状态（若原本已有则恢复原对象，若无则 pop）
        for m in MANAGED_PARSER_MODULES:
            if original_modules[m] is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = original_modules[m]

        # 5. 彻底恢复 sys.path
        sys.path[:] = original_path

        # 6. 恢复环境变量
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

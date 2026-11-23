"""
测试用仓库根路径锚点 (dev/tests/support/repo_paths.py)

测试不得依赖调用 pytest 时的工作目录：
- 读取仓库文件时使用 REPO_ROOT / "<相对路径>"；
- 需要"在新进程中导入本仓库代码"的子进程，显式传入 cwd=REPO_ROOT，
  而不是依赖全局 PYTHONPATH（避免把仓库路径泄漏给被测代码、掩盖隔离缺陷）。
"""

from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[3]

assert (REPO_ROOT / "lite_agent" / "__init__.py").is_file(), (
    f"REPO_ROOT 解析异常，未找到 lite_agent 包: {REPO_ROOT}"
)


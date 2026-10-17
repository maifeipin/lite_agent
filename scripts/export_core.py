#!/usr/bin/env python3
"""
Lite Agent 核心基础源码导出与校验工具 (scripts/export_core.py)
用于从全量仓库中提取纯净的 core 基础模式运行副本，并执行严格的安全与依赖防御检查。

支持参数:
  --manifest PATH     指定基础源码清单文件 (默认: architecture/kernel-core-manifest.txt)
  --output-dir PATH   导出目标目录 (若指定则将文件复制至该目录)
  --agent-root PATH   Agent 仓库根目录 (默认根据脚本位置自动推导)
  --verify-only       仅验证清单合法性、安全性与文件存在性，不实际写出文件
  --check-hashes      严格校验源文件 SHA-256 是否与清单一致

退出码规范:
  0: 验证或导出成功
  1: 参数错误或运行时异常
  2: 发现安全拒绝 (路径逃逸/越界符号链接/夹带业务代码/敏感数据)
  3: 发现文件缺失或 SHA-256 校验失败
  4: 发现遗漏核心基础必需依赖
"""

import argparse
import hashlib
import os
import shutil
import sys
from typing import List, Tuple, Set


FORBIDDEN_PATTERNS = [
    "features/",
    "skills/",
    "integrations/mail/",
    "config.json",
    ".env",
    "data/",
]

FORBIDDEN_EXTENSIONS = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".eml",
    ".log",
    ".pyc",
)

REQUIRED_CORE_MODULES = {
    "main.py",
    "agent.py",
    "session.py",
    "security.py",
    "requirements-core.txt",
    "contracts/message.py",
    "core/agent_runtime.py",
    "core/execution_ledger.py",
    "core/skill_engine.py",
    "services/commands.py",
    "services/routes.py",
    "services/cron.py",
    "app/bootstrap.py",
    "app/lifecycle.py",
    "channels/api.py",
    "channels/base.py",
    "integrations/notifications.py",
    "integrations/smtp/client.py",
}


def parse_args(argv: List[str] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lite Agent 基础源码导出与校验工具 (Stage 3 S3-A1)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--manifest",
        type=str,
        default=None,
        help="基础源码清单文件路径 (默认: architecture/kernel-core-manifest.txt)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="导出目标目录 (若未指定且未指定 --verify-only，则仅做校验)",
    )
    parser.add_argument(
        "--agent-root",
        type=str,
        default=None,
        help="Agent 仓库根目录 (默认自动推导)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="仅校验清单安全性与完整性，不导出文件",
    )
    parser.add_argument(
        "--check-hashes",
        action="store_true",
        help="严格校验源文件 SHA-256 是否与清单一致",
    )
    return parser.parse_args(argv)


def validate_safe_path(target_path: str, base_dir: str, name: str = "路径") -> str:
    """验证路径在规范化后严格位于 base_dir 内部，阻断符号链接越界与同名前缀逃逸。"""
    real_base = os.path.realpath(base_dir)
    real_target = os.path.realpath(target_path)
    try:
        common = os.path.commonpath([real_base, real_target])
        if common != real_base:
            raise ValueError(f"安全拒绝: {name} 越界逃逸根目录: {target_path} -> {real_target}")
    except ValueError as e:
        raise ValueError(f"安全拒绝: {name} 越界逃逸根目录: {target_path}") from e

    if real_target != real_base and not real_target.startswith(real_base + os.sep):
        raise ValueError(f"安全拒绝: {name} 越界逃逸根目录: {target_path} -> {real_target}")
    return real_target


def load_manifest(manifest_path: str) -> List[Tuple[str, str]]:
    """加载 manifest 文件，支持 '<sha256>  <relpath>' 或纯 '<relpath>' 格式。"""
    items = []
    with open(manifest_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split()
            if len(parts) >= 2 and len(parts[0]) == 64 and all(c in "0123456789abcdefABCDEF" for c in parts[0]):
                expected_sha = parts[0].lower()
                rel_path = " ".join(parts[1:])
            else:
                expected_sha = ""
                rel_path = s
            items.append((expected_sha, rel_path))
    return items


def verify_manifest_items(
    manifest_items: List[Tuple[str, str]],
    agent_root: str,
    check_hashes: bool = False,
) -> Tuple[bool, int, str]:
    """验证清单项目：安全检查、遗漏必需项检查、文件存在性检查、SHA-256 校验。"""
    if not manifest_items:
        return False, 1, "错误: 清单为空"

    found_relpaths: Set[str] = set()

    for expected_sha, rel_path in manifest_items:
        # 1. 规范化路径安全检查 (防止绝对路径、.. 目录穿越)
        if os.path.isabs(rel_path):
            return False, 2, f"安全拒绝: 清单中包含绝对路径: {rel_path}"
        norm = os.path.normpath(rel_path)
        if norm.startswith("..") or norm.split(os.sep)[0] == "..":
            return False, 2, f"安全拒绝: 清单中包含跨级上溯路径: {rel_path}"

        # 2. 业务夹带与敏感数据防御检查
        for forbidden in FORBIDDEN_PATTERNS:
            if norm == forbidden or norm.startswith(forbidden):
                return False, 2, f"安全拒绝: 清单夹带业务模块或受保护配置: {rel_path} (匹配 {forbidden})"

        for ext in FORBIDDEN_EXTENSIONS:
            if norm.endswith(ext):
                return False, 2, f"安全拒绝: 清单夹带禁止后缀的数据/缓存/日志文件: {rel_path} (匹配 {ext})"

        full_src = os.path.join(agent_root, norm)
        try:
            validate_safe_path(full_src, agent_root, "源文件")
        except ValueError as err:
            return False, 2, str(err)

        if not os.path.isfile(full_src):
            return False, 3, f"错误: 清单文件不存在: {rel_path} (查找路径: {full_src})"

        if check_hashes and not expected_sha:
            return False, 3, f"校验错误: 缺少 SHA-256: {rel_path}"
        if check_hashes and expected_sha:
            with open(full_src, "rb") as f:
                actual_sha = hashlib.sha256(f.read()).hexdigest()
            if actual_sha != expected_sha:
                return False, 3, f"校验错误: SHA-256 不匹配: {rel_path} (预期: {expected_sha}, 实际: {actual_sha})"

        found_relpaths.add(norm)

    # 3. 遗漏必需核心依赖检查
    missing_required = REQUIRED_CORE_MODULES - found_relpaths
    if missing_required:
        return False, 4, f"错误: 清单遗漏关键核心基础模块: {sorted(list(missing_required))}"

    return True, 0, f"验证通过: 共 {len(manifest_items)} 项核心文件，安全与完整性检查合格"


def export_core_files(
    manifest_items: List[Tuple[str, str]],
    agent_root: str,
    output_dir: str,
) -> Tuple[bool, int, str]:
    """将清单文件复制到目标目录，严格防止目标目录逃逸与符号链接穿透。"""
    try:
        os.makedirs(output_dir, exist_ok=True)
        safe_output_dir = validate_safe_path(output_dir, output_dir, "导出目录")
    except Exception as err:
        return False, 2, f"导出目录初始化失败: {err}"

    copied_count = 0
    for _, rel_path in manifest_items:
        norm = os.path.normpath(rel_path)
        src_path = os.path.join(agent_root, norm)
        dst_path = os.path.join(safe_output_dir, norm)

        try:
            # 创建目录之前检查完整目标，避免穿过父目录或最终文件的软链接。
            validate_safe_path(src_path, agent_root, "源文件")
            validate_safe_path(dst_path, safe_output_dir, "目标文件")
            dst_parent = os.path.dirname(dst_path)
            os.makedirs(dst_parent, exist_ok=True)
            validate_safe_path(dst_path, safe_output_dir, "目标文件")
            shutil.copy2(src_path, dst_path)
        except (OSError, ValueError) as err:
            return False, 2, f"导出拒绝: {rel_path}: {err}"
        copied_count += 1

    return True, 0, f"成功导出 {copied_count} 个核心文件至 {safe_output_dir}"


def main(argv: List[str] = None):
    args = parse_args(argv)

    agent_root = os.path.abspath(args.agent_root) if args.agent_root else os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    )
    if not os.path.isdir(agent_root):
        print(f"❌ 找不到 Agent 根目录: {agent_root}", file=sys.stderr)
        sys.exit(1)

    manifest_path = os.path.abspath(args.manifest) if args.manifest else os.path.join(
        agent_root, "architecture", "kernel-core-manifest.txt"
    )
    if not os.path.isfile(manifest_path):
        print(f"❌ 找不到 manifest 文件: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    try:
        manifest_items = load_manifest(manifest_path)
    except Exception as e:
        print(f"❌ 读取 manifest 异常: {e}", file=sys.stderr)
        sys.exit(1)

    ok, exit_code, msg = verify_manifest_items(
        manifest_items,
        agent_root,
        check_hashes=args.check_hashes,
    )
    if not ok:
        print(f"❌ {msg}", file=sys.stderr)
        sys.exit(exit_code)

    print(f"✅ {msg}")

    if args.verify_only or not args.output_dir:
        sys.exit(0)

    # 执行导出
    ok, exit_code, msg = export_core_files(
        manifest_items,
        agent_root,
        args.output_dir,
    )
    if not ok:
        print(f"❌ {msg}", file=sys.stderr)
        sys.exit(exit_code)

    print(f"🎉 {msg}")
    sys.exit(0)


if __name__ == "__main__":
    main()

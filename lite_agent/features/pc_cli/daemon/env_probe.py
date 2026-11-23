"""
本机环境探测与安全目录浏览 (daemon.env_probe).
负责：
1. 补齐 macOS launchd 后台运行缺失的 Homebrew / NVM PATH；
2. 检测本机可用的 codex / agy 可执行文件；
3. 基于 workspace_roots 白名单做 realpath 校验，防路径穿越。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from typing import Dict, List, Optional, Tuple


_MACOS_COMMON_BINS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.npm-global/bin"),
)


def build_engine_env(extra_path: Optional[List[str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    paths = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    candidates = list(extra_path or [])
    if sys.platform == "darwin":
        candidates.extend(_MACOS_COMMON_BINS)
    for p in candidates:
        expanded = os.path.expanduser(p)
        if expanded and expanded not in paths and os.path.isdir(expanded):
            paths.append(expanded)
    env["PATH"] = os.pathsep.join(paths)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def resolve_executable(cmd: str, env: Dict[str, str]) -> Optional[str]:
    cleaned = cmd.strip().strip('"').strip("'")
    if not cleaned:
        return None
    return shutil.which(cleaned, path=env.get("PATH"))


def _unwrap_npm_cmd_shim(shim_path: str) -> Optional[str]:
    """借鉴 tarobird engine-process.ts：解析 npm .cmd shim 的真实 JS 入口，避免 cmd.exe 破坏管道与信号。"""
    try:
        if os.path.getsize(shim_path) > 16384:
            return None
        with open(shim_path, "r", encoding="utf-8", errors="ignore") as f:
            body = f.read()
        m = re.search(r'"%~?dp0%?[\\/]([^"\r\n]+\.(?:js|mjs|cjs))"', body, re.IGNORECASE)
        if not m:
            return None
        rel = m.group(1).replace("\\", os.sep).replace("/", os.sep)
        candidate = os.path.normpath(os.path.join(os.path.dirname(shim_path), rel))
        return candidate if os.path.isfile(candidate) else None
    except OSError:
        return None


def build_spawn_argv(command: str, args: List[str], env: Dict[str, str], platform: Optional[str] = None) -> List[str]:
    """构建跨平台安全的 create_subprocess_exec 参数数组（fail-closed：严禁回退到 cmd.exe / powershell 解释器）。"""
    plat = platform or sys.platform
    resolved = resolve_executable(command, env) or command.strip().strip('"').strip("'")
    if plat != "win32":
        return [resolved, *args]

    lower = resolved.lower()
    if lower.endswith(".cmd") or lower.endswith(".bat"):
        js_entry = _unwrap_npm_cmd_shim(resolved)
        node_bin = resolve_executable("node", env)
        if js_entry and node_bin:
            return [node_bin, js_entry, *args]
        raise ValueError(f"拒绝通过命令解释器执行无法拆包的脚本: {resolved}")
    if lower.endswith(".ps1"):
        raise ValueError(f"拒绝通过 PowerShell 解释器执行脚本: {resolved}")
    return [resolved, *args]


def detect_available_engines(codex_bin: str, agy_bin: str, env: Dict[str, str]) -> List[str]:
    engines: List[str] = []
    if resolve_executable(codex_bin, env):
        engines.append("codex")
    if resolve_executable(agy_bin, env):
        engines.append("agy")
    return engines


def is_path_within_roots(target_path: str, roots: List[str]) -> bool:
    if not target_path or not roots:
        return False
    real_target = os.path.realpath(os.path.expanduser(target_path))
    for root in roots:
        real_root = os.path.realpath(os.path.expanduser(root))
        try:
            if os.path.commonpath([real_target, real_root]) == real_root:
                return True
        except ValueError:
            # Windows 跨盘符比较会抛 ValueError
            continue
    return False


def list_safe_dirs(req_path: str, roots: List[str], max_entries: int = 100) -> Tuple[bool, List[Dict[str, str]], str]:
    """安全列出白名单根目录下的子目录。req_path 为空时返回根目录列表。"""
    if not req_path.strip():
        entries = []
        for r in roots:
            real_r = os.path.realpath(os.path.expanduser(r))
            if os.path.isdir(real_r):
                entries.append({"name": os.path.basename(real_r) or real_r, "path": real_r})
        return True, entries, ""

    if not is_path_within_roots(req_path, roots):
        return False, [], f"路径不在允许的工作目录白名单内: {req_path}"

    real_dir = os.path.realpath(os.path.expanduser(req_path))
    if not os.path.isdir(real_dir):
        return False, [], f"目录不存在: {req_path}"

    entries = []
    try:
        for name in sorted(os.listdir(real_dir)):
            if name.startswith(".") or name in ("node_modules", "__pycache__", ".git"):
                continue
            full = os.path.join(real_dir, name)
            if os.path.isdir(full):
                entries.append({"name": name, "path": full})
                if len(entries) >= max_entries:
                    break
    except OSError as e:
        return False, [], f"读取目录失败: {e}"
    return True, entries, ""

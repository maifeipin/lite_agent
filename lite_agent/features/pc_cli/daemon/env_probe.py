"""
本机环境探测与安全目录浏览 (daemon.env_probe).
负责：
1. 补齐 macOS launchd 后台运行缺失的 Homebrew / NVM PATH；
2. 检测本机可用的 codex / agy 可执行文件；
3. 基于 workspace_roots 白名单做 realpath 校验，防路径穿越。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

_SAFE_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_CODEX_TOML_MODEL_RE = re.compile(r'^\s*model\s*=\s*["\']([^"\']+)["\']')


_MACOS_COMMON_BINS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.npm-global/bin"),
)

# Desktop 通过这些进程局部变量把其子进程绑定到应用运行时/通信管道。
# Daemon 启动的是独立 CLI，不能沿用父桌面会话的服务端点；否则 Windows
# 会因端点不可用而拒绝 helper fallback，即使本机的原生沙箱可以正常执行。
# 保留 CODEX_HOME、认证、代理与权限配置等用户设置。
_CODEX_DESKTOP_SESSION_ENV = (
    "CODEX_APP_RUNTIME_CLI_PATH",
    "CODEX_APP_TOOLS_PIPE_PATH",
    "CODEX_WINDOWS_REGISTERED_CORE",
    "CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
)


def build_engine_env(extra_path: Optional[List[str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    for name in _CODEX_DESKTOP_SESSION_ENV:
        env.pop(name, None)
    paths = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    candidates = list(extra_path or [])
    if sys.platform == "darwin":
        candidates.extend(_MACOS_COMMON_BINS)
    elif sys.platform == "win32":
        # 后台进程不执行 PowerShell profile；使用 fnm 的持久 default alias，
        # 不依赖某个终端的临时 fnm_multishells 目录。
        fnm_roots = [env.get("FNM_DIR", "")]
        for key in ("APPDATA", "LOCALAPPDATA"):
            if env.get(key):
                fnm_roots.append(os.path.join(env[key], "fnm"))
        candidates.extend(os.path.join(root, "aliases", "default") for root in fnm_roots if root)
        if env.get("APPDATA"):
            candidates.append(os.path.join(env["APPDATA"], "npm"))
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
        sibling_node = os.path.join(os.path.dirname(resolved), "node.exe")
        node_bin = sibling_node if os.path.isfile(sibling_node) else resolve_executable("node", env)
        if js_entry and node_bin:
            return [node_bin, js_entry, *args]
        raise ValueError(f"拒绝通过命令解释器执行无法拆包的脚本: {resolved}")
    if lower.endswith(".ps1"):
        raise ValueError(f"拒绝通过 PowerShell 解释器执行脚本: {resolved}")
    return [resolved, *args]


def detect_available_engines(codex_bin: str, agy_bin: str, env: Dict[str, str]) -> List[str]:
    engines: List[str] = []
    for name, command in (("codex", codex_bin), ("agy", agy_bin)):
        if resolve_executable(command, env):
            try:
                build_spawn_argv(command, [], env)
            except ValueError:
                continue
            engines.append(name)
    return engines


def _read_codex_default_model(codex_home: str) -> str:
    """从 ~/.codex/config.toml 顶层读取默认 model 配置（忽略子表中的 model 字段）。"""
    cfg_path = os.path.join(codex_home, "config.toml")
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("["):
                    break
                m = _CODEX_TOML_MODEL_RE.match(stripped)
                if m:
                    candidate = m.group(1).strip()
                    if _SAFE_MODEL_ID_RE.match(candidate):
                        return candidate
    except OSError:
        pass
    return ""


def discover_codex_models(
    codex_home: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    max_models: int = 50,
) -> List[Dict[str, Any]]:
    """读取本机 Codex CLI 缓存的可用模型列表（~/.codex/models_cache.json + config.toml）。"""
    base_dir = (
        codex_home
        or (env or os.environ).get("CODEX_HOME")
        or os.path.expanduser("~/.codex")
    )
    default_model = _read_codex_default_model(base_dir)
    cache_path = os.path.join(base_dir, "models_cache.json")
    models: List[Dict[str, Any]] = []
    seen: set[str] = set()

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw_items = data.get("models") if isinstance(data, dict) else None
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                visibility = str(item.get("visibility") or "list").strip().lower()
                if visibility and visibility != "list":
                    continue
                slug = str(item.get("slug") or item.get("id") or "").strip()
                if not slug or not _SAFE_MODEL_ID_RE.match(slug) or slug in seen:
                    continue
                seen.add(slug)
                display_name = str(item.get("display_name") or slug).strip()[:120]
                efforts: List[str] = []
                raw_levels = item.get("supported_reasoning_levels")
                if isinstance(raw_levels, list):
                    for lv in raw_levels:
                        lv_name = str(lv.get("effort") if isinstance(lv, dict) else lv).strip().lower()
                        if lv_name in ("low", "medium", "high", "xhigh") and lv_name not in efforts:
                            efforts.append(lv_name)
                entry: Dict[str, Any] = {
                    "id": slug,
                    "name": display_name,
                    "is_default": slug == default_model,
                }
                if efforts:
                    entry["reasoning_efforts"] = efforts
                models.append(entry)
                if len(models) >= max_models:
                    break
    except (OSError, ValueError, TypeError):
        pass

    if default_model and default_model not in seen:
        models.insert(0, {"id": default_model, "name": default_model, "is_default": True})
    return models


def discover_engine_models(
    engines: List[str],
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """探测本机已启用引擎的可用模型清单。"""
    result: Dict[str, List[Dict[str, Any]]] = {}
    for eng in engines:
        if eng == "codex":
            result["codex"] = discover_codex_models(env=env)
        elif eng == "agy":
            # AGY CLI 当前无公开本地模型缓存文件，返回空列表由前端提供默认/自定义输入
            result["agy"] = []
    return result


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

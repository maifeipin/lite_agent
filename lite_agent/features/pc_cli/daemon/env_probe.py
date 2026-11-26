"""
本机环境探测与安全目录浏览 (daemon.env_probe).
负责：
1. 补齐 macOS launchd 后台运行缺失的 Homebrew / NVM PATH；
2. 检测本机可用的 codex / agy 可执行文件；
3. 基于 workspace_roots 白名单做 realpath 校验，防路径穿越。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shutil
import stat
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


@dataclass(frozen=True)
class PinnedWorkspaceRoot:
    """守护进程启动时固定的可信白名单根目录身份（跨 POSIX / Windows 共享）。"""

    real_path: str
    dev: int
    ino: int
    fd: Optional[int] = None


def pin_workspace_roots(roots: List[str]) -> List[PinnedWorkspaceRoot]:
    """在守护进程载入配置时一次性解析并固定白名单根目录身份（路径 + 设备/inode + POSIX 目录句柄）。"""
    pinned: List[PinnedWorkspaceRoot] = []
    seen: set[str] = set()
    is_posix_dirfd = bool(os.name == "posix" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"))
    for root in roots:
        cleaned = (root or "").strip()
        if not cleaned:
            continue
        real_root = os.path.realpath(os.path.expanduser(cleaned))
        if real_root in seen or not os.path.isdir(real_root):
            continue
        try:
            lst = os.lstat(real_root)
            if stat.S_ISLNK(lst.st_mode) or not stat.S_ISDIR(lst.st_mode) or getattr(lst, "st_reparse_tag", 0):
                continue
            if is_posix_dirfd:
                fd = os.open(real_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                st = os.fstat(fd)
                pinned.append(PinnedWorkspaceRoot(real_path=real_root, dev=st.st_dev, ino=st.st_ino, fd=fd))
            else:
                st = os.stat(real_root)
                pinned.append(PinnedWorkspaceRoot(real_path=real_root, dev=st.st_dev, ino=st.st_ino, fd=None))
            seen.add(real_root)
        except OSError:
            continue
    return pinned


def close_pinned_roots(pinned: List[PinnedWorkspaceRoot]) -> None:
    """释放已固定的白名单根目录文件描述符。"""
    for item in list(pinned):
        if item.fd is not None and item.fd >= 0:
            try:
                os.close(item.fd)
            except OSError:
                pass
    pinned.clear()


def _is_pinned_root_intact(root: PinnedWorkspaceRoot) -> bool:
    """核验固定的白名单根目录当前未被替换为符号链接/目录联接，且底层 (st_dev, st_ino) 身份未变。"""
    try:
        lst = os.lstat(root.real_path)
        if stat.S_ISLNK(lst.st_mode) or not stat.S_ISDIR(lst.st_mode):
            return False
        if getattr(lst, "st_reparse_tag", 0):
            return False
        st = os.stat(root.real_path)
        if (root.dev or root.ino) and (st.st_dev != root.dev or st.st_ino != root.ino):
            return False
        return True
    except OSError:
        return False


def is_path_within_roots(
    target_path: str,
    roots: List[str],
    *,
    pinned_roots: Optional[List[PinnedWorkspaceRoot]] = None,
) -> bool:
    if not target_path:
        return False
    real_target = os.path.realpath(os.path.expanduser(target_path))
    if pinned_roots is not None:
        for p in pinned_roots:
            if not _is_pinned_root_intact(p):
                continue
            try:
                if os.path.commonpath([real_target, p.real_path]) == p.real_path:
                    return True
            except ValueError:
                continue
        return False

    if not roots:
        return False
    for root in roots:
        real_root = os.path.realpath(os.path.expanduser(root))
        try:
            if os.path.commonpath([real_target, real_root]) == real_root:
                return True
        except ValueError:
            # Windows 跨盘符比较会抛 ValueError
            continue
    return False


def resolve_safe_cwd(
    target_path: str,
    roots: List[str],
    *,
    pinned_roots: Optional[List[PinnedWorkspaceRoot]] = None,
) -> Optional[str]:
    """校验并返回真实存在且位于可信白名单根目录内的规范化工作目录；不合法或根目录被篡改时返回 None。"""
    if not is_path_within_roots(target_path, roots, pinned_roots=pinned_roots):
        return None
    real_target = os.path.realpath(os.path.expanduser(target_path))
    if not os.path.isdir(real_target):
        return None
    if not is_path_within_roots(real_target, roots, pinned_roots=pinned_roots):
        return None
    return real_target


def _resolve_relative_parts_under_pinned(
    target_path: str,
    pinned_roots: List[PinnedWorkspaceRoot],
) -> Optional[Tuple[str, int, List[str]]]:
    """仅基于启动时固定的可信根目录身份解析相对路径段，绝不重新调用 realpath 解析根目录。"""
    abs_target = os.path.normpath(os.path.abspath(os.path.expanduser(target_path)))
    for p in pinned_roots:
        if p.fd is None or p.fd < 0 or not _is_pinned_root_intact(p):
            continue
        try:
            if os.path.commonpath([abs_target, p.real_path]) == p.real_path:
                rel = os.path.relpath(abs_target, p.real_path)
                if rel == ".":
                    return p.real_path, p.fd, []
                parts = [seg for seg in rel.split(os.sep) if seg and seg != "."]
                if any(seg == ".." for seg in parts):
                    return None
                return p.real_path, p.fd, parts
        except ValueError:
            continue
    return None


def _resolve_relative_parts_under_raw_roots(target_path: str, roots: List[str]) -> Optional[Tuple[str, List[str]]]:
    """无固定句柄时按配置原始绝对路径（不经 realpath 跟随符号链接）计算相对路径段。"""
    abs_target = os.path.normpath(os.path.abspath(os.path.expanduser(target_path)))
    for root in roots:
        abs_root = os.path.normpath(os.path.abspath(os.path.expanduser(root)))
        if not abs_root:
            continue
        try:
            if os.path.commonpath([abs_target, abs_root]) == abs_root:
                rel = os.path.relpath(abs_target, abs_root)
                if rel == ".":
                    return abs_root, []
                parts = [p for p in rel.split(os.sep) if p and p != "."]
                if any(p == ".." for p in parts):
                    return None
                return abs_root, parts
        except ValueError:
            continue
    return None


def ensure_safe_dir(
    req_path: str,
    roots: List[str],
    *,
    pinned_roots: Optional[List[PinnedWorkspaceRoot]] = None,
) -> Tuple[bool, str, str]:
    """从启动时固定的可信白名单根目录句柄出发逐级安全创建目录（仅 POSIX dir_fd+O_NOFOLLOW 启用；Windows 保持 fail-closed）。"""
    cleaned = (req_path or "").strip()
    if not cleaned:
        return False, "", "目录路径不能为空"

    if not (os.name == "posix" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")):
        return False, "", "当前平台暂不支持自动创建不存在的工作目录，请先在本机创建目录后再点选"

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    cur_fd = -1
    cur_path = ""
    parts: List[str] = []

    try:
        if pinned_roots is not None:
            matched = _resolve_relative_parts_under_pinned(cleaned, pinned_roots)
            if matched is None:
                return False, "", f"路径不在已固定的工作目录白名单内: {cleaned}"
            pinned_root, root_fd, parts = matched
            cur_fd = os.dup(root_fd)
            cur_path = pinned_root
        else:
            anchored = _resolve_relative_parts_under_raw_roots(cleaned, roots)
            if anchored is None:
                return False, "", f"路径不在允许的工作目录白名单内: {cleaned}"
            abs_root, parts = anchored
            # 未传入固定句柄时，直接按配置原始路径从 / 起逐级以 O_NOFOLLOW 打开（拒绝已被替换为符号链接的根目录）
            root_parts = [p for p in abs_root.split(os.sep) if p]
            cur_fd = os.open(os.sep, flags)
            cur_path = os.sep
            for rpart in root_parts:
                try:
                    next_fd = os.open(rpart, flags, dir_fd=cur_fd)
                except OSError as e:
                    return False, "", f"拒绝通过符号链接或无效白名单根目录创建目录: {abs_root} ({e})"
                os.close(cur_fd)
                cur_fd = next_fd
                cur_path = os.path.join(cur_path, rpart)

        for part in parts:
            try:
                os.mkdir(part, dir_fd=cur_fd)
            except FileExistsError:
                pass
            try:
                next_fd = os.open(part, flags, dir_fd=cur_fd)
            except OSError as e:
                return False, "", f"拒绝通过符号链接或非目录路径创建目录: {os.path.join(cur_path, part)} ({e})"
            os.close(cur_fd)
            cur_fd = next_fd
            cur_path = os.path.join(cur_path, part)
        return True, cur_path, ""
    except OSError as e:
        return False, "", f"创建目录失败: {e}"
    finally:
        if cur_fd >= 0:
            try:
                os.close(cur_fd)
            except OSError:
                pass


def list_safe_dirs(
    req_path: str,
    roots: List[str],
    max_entries: int = 100,
    *,
    create_if_missing: bool = False,
    pinned_roots: Optional[List[PinnedWorkspaceRoot]] = None,
) -> Tuple[bool, List[Dict[str, str]], str]:
    """安全列出白名单根目录下的子目录。req_path 为空时返回根目录列表。"""
    if not req_path.strip():
        entries = []
        if pinned_roots is not None:
            for p in pinned_roots:
                if _is_pinned_root_intact(p):
                    entries.append({"name": os.path.basename(p.real_path) or p.real_path, "path": p.real_path})
            return True, entries, ""
        for r in roots:
            real_r = os.path.realpath(os.path.expanduser(r))
            if os.path.isdir(real_r):
                entries.append({"name": os.path.basename(real_r) or real_r, "path": real_r})
        return True, entries, ""

    if not is_path_within_roots(req_path, roots, pinned_roots=pinned_roots):
        return False, [], f"路径不在允许的工作目录白名单内: {req_path}"

    real_dir = os.path.realpath(os.path.expanduser(req_path))
    if not os.path.isdir(real_dir):
        if create_if_missing:
            ok_mk, created_dir, mk_err = ensure_safe_dir(
                req_path,
                roots,
                pinned_roots=pinned_roots,
            )
            if not ok_mk:
                return False, [], mk_err
            real_dir = created_dir
        else:
            return False, [], f"目录不存在: {req_path}"
    elif not is_path_within_roots(real_dir, roots, pinned_roots=pinned_roots):
        return False, [], f"路径不在允许的工作目录白名单内: {req_path}"

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

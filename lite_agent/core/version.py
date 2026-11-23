"""
系统构建版本元数据 (core.version)。

只提供服务端可确定的信息：
- build_sha: 优先读取环境变量 LITE_AGENT_BUILD_SHA（镜像/发布目录构建时注入），
  其次读取 git HEAD，均失败时为 "unknown"。
- api_version: 对外 API 兼容版本。

Android (versionName/versionCode) 与 Web 资源版本由各自构建链独立维护，
此处不重复硬编码，避免多处版本号漂移。
"""

import os
import subprocess
import threading
from typing import Dict, Optional

API_VERSION = "v1"

_cache_lock = threading.Lock()
_cached_sha: Optional[str] = None


def _read_git_sha(repo_root: Optional[str]) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=2,
        )
        sha = res.stdout.strip()
        if res.returncode == 0 and sha:
            return sha
    except Exception:
        pass
    return None


def get_build_sha(repo_root: Optional[str] = None) -> str:
    """返回当前进程的构建 SHA（进程内缓存，进程启动后不随工作区变化）。"""
    global _cached_sha
    with _cache_lock:
        if _cached_sha is None:
            env_sha = os.environ.get("LITE_AGENT_BUILD_SHA", "").strip()
            if env_sha:
                _cached_sha = env_sha[:12]
            else:
                if repo_root is None:
                    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                _cached_sha = _read_git_sha(repo_root) or "unknown"
        return _cached_sha


def get_version_info() -> Dict[str, str]:
    return {"build_sha": get_build_sha(), "api_version": API_VERSION}


def _reset_cache_for_tests() -> None:
    global _cached_sha
    with _cache_lock:
        _cached_sha = None

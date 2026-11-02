"""
系统版本元数据服务 (core.version)。
提供组件版本、构建 SHA 与运行时元数据。
"""

import os
import subprocess
from typing import Dict, Any, Optional

COMPONENT_VERSIONS = {
    "core": "1.4.0",
    "android": "1.3.1",
    "web_dashboard": "1.2.0",
    "api": "v1",
}

def get_git_commit_sha(repo_root: Optional[str] = None) -> str:
    """获取当前 HEAD 提交短哈希"""
    try:
        cmd = ["git", "rev-parse", "--short=8", "HEAD"]
        res = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return "release"

def get_system_version_info(repo_root: Optional[str] = None) -> Dict[str, Any]:
    """获取统一版本展示结构"""
    sha = get_git_commit_sha(repo_root)
    return {
        "core_version": COMPONENT_VERSIONS["core"],
        "android_version": COMPONENT_VERSIONS["android"],
        "web_dashboard_version": COMPONENT_VERSIONS["web_dashboard"],
        "api_version": COMPONENT_VERSIONS["api"],
        "git_commit": sha,
        "release_tag": f"v{COMPONENT_VERSIONS['core']}+{sha}",
    }

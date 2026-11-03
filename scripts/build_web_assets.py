#!/usr/bin/env python3
"""
自动化静态资源指纹构建脚本 (build_web_assets.py)。
计算 web_dashboard 内部所有本地 JS / CSS 文件的内容短哈希，并自动重写输出目录（如发布目录）下的 index.html。
严禁在开发期直接覆盖开发源文件破坏 manifest 校验，支持源目录发布构建或就地带 hash 生成。
"""

import os
import re
import sys
import hashlib
import shutil
import subprocess
from typing import Optional
from pathlib import Path

def hash_file(filepath: Path) -> str:
    """计算文件 SHA-256 前 8 位作为版本号"""
    return hashlib.sha256(filepath.read_bytes()).hexdigest()[:8]

def get_git_sha(repo_root: Optional[Path] = None) -> str:
    """获取构建 Git Short SHA (优先环境变量 LITE_AGENT_BUILD_SHA，其次 git rev-parse)"""
    env_sha = os.environ.get("LITE_AGENT_BUILD_SHA", "").strip()
    if env_sha:
        return env_sha[:8]
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=2,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"

def build_assets(src_dir: Path, dist_dir: Path, build_sha: Optional[str] = None):
    if not src_dir.exists():
        raise FileNotFoundError(f"Source directory not found: {src_dir}")

    # 如果 dist_dir 与 src_dir 不同，则先同步全部文件到 dist_dir (安全覆盖，绝不删除目录本身)
    if dist_dir.resolve() != src_dir.resolve():
        shutil.copytree(src_dir, dist_dir, dirs_exist_ok=True)
    
    index_html = dist_dir / "index.html"
    if not index_html.exists():
        print(f"Warning: {index_html} does not exist.")
        return

    content = index_html.read_text(encoding="utf-8")

    # 替换样式表: href="style.css(?v=...)?"
    def replace_link(match):
        rel_path = match.group(1)
        if rel_path.startswith("http://") or rel_path.startswith("https://") or rel_path.startswith("//"):
            return match.group(0)
        clean_path = rel_path.split("?")[0]
        target_file = dist_dir / clean_path
        if target_file.exists():
            h = hash_file(target_file)
            return f'href="{clean_path}?v={h}"'
        return match.group(0)

    # 替换脚本: src="xxx.js(?v=...)?"
    def replace_script(match):
        rel_path = match.group(1)
        if rel_path.startswith("http://") or rel_path.startswith("https://") or rel_path.startswith("//"):
            return match.group(0)
        clean_path = rel_path.split("?")[0]
        target_file = dist_dir / clean_path
        if target_file.exists():
            h = hash_file(target_file)
            return f'src="{clean_path}?v={h}"'
        return match.group(0)

    new_content = re.sub(r'href=["\']([^"\']+\.css(?:\?[^"\']*)?)["\']', replace_link, content)
    new_content = re.sub(r'src=["\']([^"\']+\.js(?:\?[^"\']*)?)["\']', replace_script, new_content)

    # 注入/更新构建版本号
    if build_sha is None:
        build_sha = get_git_sha(src_dir.parent)

    meta_tag = f'<meta name="build-sha" content="{build_sha}">'
    if 'name="build-sha"' in new_content:
        new_content = re.sub(r'<meta\s+name=["\']build-sha["\']\s+content=["\'][^"\']*["\']>', meta_tag, new_content)
    elif '</head>' in new_content:
        new_content = new_content.replace('</head>', f'    {meta_tag}\n</head>')

    # 注入/更新静态版本占位徽章 (如果有)
    def replace_version_badge(match):
        pre = match.group(1)
        post = match.group(2)
        clean_pre = re.sub(r'data-web-sha=["\'][^"\']*["\']', '', pre)
        clean_post = re.sub(r'data-web-sha=["\'][^"\']*["\']', '', post)
        return f'<span {clean_pre.rstrip()} id="agent-build-version" data-web-sha="{build_sha}"{clean_post}>v1.0.0 ({build_sha})</span>'

    new_content = re.sub(
        r'<span\s+([^>]*?)id=["\']agent-build-version["\']([^>]*?)>.*?</span>',
        replace_version_badge,
        new_content,
    )

    index_html.write_text(new_content, encoding="utf-8")
    print(f"✅ Web 静态资源指纹与版本号注入完成: {index_html} (Build: {build_sha})")

if __name__ == "__main__":
    base = Path(__file__).resolve().parent.parent / "web_dashboard"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else base
    sha = sys.argv[2] if len(sys.argv) > 2 else None
    build_assets(base, out, build_sha=sha)

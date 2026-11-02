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
from pathlib import Path

def hash_file(filepath: Path) -> str:
    """计算文件 SHA-256 前 8 位作为版本号"""
    return hashlib.sha256(filepath.read_bytes()).hexdigest()[:8]

def build_assets(src_dir: Path, dist_dir: Path):
    if not src_dir.exists():
        raise FileNotFoundError(f"Source directory not found: {src_dir}")

    # 如果 dist_dir 与 src_dir 不同，则先同步全部文件到 dist_dir
    if dist_dir.resolve() != src_dir.resolve():
        if dist_dir.exists():
            shutil.rmtree(dist_dir)
        shutil.copytree(src_dir, dist_dir)
    
    index_html = dist_dir / "index.html"
    if not index_html.exists():
        print(f"Warning: {index_html} does not exist.")
        return

    content = index_html.read_text(encoding="utf-8")

    # 替换样式表: href="style.css(?v=...)?"
    def replace_link(match):
        rel_path = match.group(1)
        if rel_path.startswith("http://") or rel_path.startswith("https://"):
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
        if rel_path.startswith("http://") or rel_path.startswith("https://"):
            return match.group(0)
        clean_path = rel_path.split("?")[0]
        target_file = dist_dir / clean_path
        if target_file.exists():
            h = hash_file(target_file)
            return f'src="{clean_path}?v={h}"'
        return match.group(0)

    new_content = re.sub(r'href=["\']([^"\']+\.css(?:\?[^"\']*)?)["\']', replace_link, content)
    new_content = re.sub(r'src=["\']([^"\']+\.js(?:\?[^"\']*)?)["\']', replace_script, new_content)

    index_html.write_text(new_content, encoding="utf-8")
    print(f"✅ Web 静态资源指纹注入完成: {index_html}")

if __name__ == "__main__":
    base = Path(__file__).resolve().parent.parent / "web_dashboard"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else base
    build_assets(base, out)

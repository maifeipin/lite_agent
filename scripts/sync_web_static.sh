#!/usr/bin/env bash
# ==============================================================================
# scripts/sync_web_static.sh
#
# 自动化 Web 前端静态资源一键同步与 Cache-Busting 指纹构建脚本
#
# 功能：
# 1. 将 web_dashboard/ 下的所有资源（HTML、CSS、JS、modules/、components/ 等）
#    完整同步至目标发布目录（默认: /var/www/dashboard/dist）；
# 2. 调用 scripts/build_web_assets.py 重新计算本地 JS/CSS 的 SHA-256 内容哈希，
#    自动重写发布目录下的 index.html（附带 ?v=<hash> 与 Git Commit Version）；
# 3. 校验发布完整性（确保各核心模块及指纹注入均正确无误）。
#
# 使用方式：
#   ./scripts/sync_web_static.sh                      # 本地或 VPS 默认目标 (/var/www/dashboard/dist)
#   ./scripts/sync_web_static.sh /custom/target/dir   # 自定义本地目标目录
#   ./scripts/sync_web_static.sh --remote [vps1]      # 从开发机远程同步到 VPS 并自动构建
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$REPO_ROOT/web_dashboard"

if [ ! -d "$SRC_DIR" ]; then
    echo "❌ 错误: 前端源目录不存在: $SRC_DIR" >&2
    exit 1
fi

# 处理 --remote 模式 (从本地向远端主机同步，绑定本地构建版本)
if [ "${1:-}" = "--remote" ]; then
    REMOTE_HOST="${2:-vps1}"
    REMOTE_AGENT_DIR="/home/liteagent/lite_agent"
    REMOTE_TARGET="/var/www/dashboard/dist"
    echo "🌐 [Remote] 开始向 $REMOTE_HOST 远端发布静态资源..."

    # 1. 精确获取本地构建 SHA，绝不依赖远端 Git 状态
    LOCAL_SHA="${LITE_AGENT_BUILD_SHA:-$(git -C "$REPO_ROOT" rev-parse --short=8 HEAD 2>/dev/null || echo 'unknown')}"
    echo "📌 [Remote] 本次发布精确绑定本地构建版本: $LOCAL_SHA"

    # 2. 在本地创建干净的暂存构建目录执行指纹构建
    TMP_DIST="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIST"' EXIT

    echo "🔨 [Remote] 在本地预构建静态产物与 Cache-Busting 指纹 (Build SHA: $LOCAL_SHA)..."
    python3 "$SCRIPT_DIR/build_web_assets.py" "$TMP_DIST" "$LOCAL_SHA" --src "$SRC_DIR"

    # 3. 校验本地构建产物完整性与版本注入
    test -f "$TMP_DIST/index.html"
    test -f "$TMP_DIST/main.js"
    test -f "$TMP_DIST/style.css"
    test -d "$TMP_DIST/modules"
    test -d "$TMP_DIST/components"
    grep -q "style.css?v=" "$TMP_DIST/index.html"
    grep -q "$LOCAL_SHA" "$TMP_DIST/index.html"

    # 4. 直接将预构建产物同步至远端 Nginx 静态目录 (不覆盖远端用户属主)
    echo "🚀 [Remote] 同步本地预构建产物至 $REMOTE_HOST:$REMOTE_TARGET/ ..."
    rsync -avz --delete --no-owner --no-group \
        --exclude='.DS_Store' \
        --exclude='*.bak*' \
        "$TMP_DIST/" "$REMOTE_HOST:$REMOTE_TARGET/"

    # 5. 同时更新远端工作区内前端源文件
    echo "🔄 [Remote] 同步前端源文件至 $REMOTE_HOST:$REMOTE_AGENT_DIR/web_dashboard/ ..."
    rsync -avz --delete --no-owner --no-group \
        --exclude='.DS_Store' \
        --exclude='*.bak*' \
        --exclude='__pycache__' \
        "$SRC_DIR/" "$REMOTE_HOST:$REMOTE_AGENT_DIR/web_dashboard/"

    # 6. 在远端执行版本自检（必须开启 set -euo pipefail，确保任何校验失败时 SSH 退出非 0）
    ssh "$REMOTE_HOST" "su - liteagent -c '
        set -euo pipefail
        TARGET=\"$REMOTE_TARGET\"
        test -f \"\$TARGET/index.html\"
        test -f \"\$TARGET/main.js\"
        test -f \"\$TARGET/style.css\"
        if ! grep -q \"$LOCAL_SHA\" \"\$TARGET/index.html\"; then
            echo \"❌ [Remote VPS] 版本自检失败: 未在 \$TARGET/index.html 中找到构建指纹 $LOCAL_SHA\" >&2
            exit 1
        fi
        echo \"✅ [Remote VPS] 远端静态资源版本校验成功: Build SHA = $LOCAL_SHA\"
    '"
    echo "🎉 [Remote] $REMOTE_HOST 远端前端发布全部完成 (版本确认为本地提交: $LOCAL_SHA)！"
    exit 0
fi

# 本地发布模式
TARGET_DIR="${1:-/var/www/dashboard/dist}"
LOCAL_SHA="${LITE_AGENT_BUILD_SHA:-$(git -C "$REPO_ROOT" rev-parse --short=8 HEAD 2>/dev/null || echo 'unknown')}"
echo "📦 [Local] 开始同步前端静态资源: $SRC_DIR -> $TARGET_DIR (Build SHA: $LOCAL_SHA)"

if [ ! -d "$TARGET_DIR" ]; then
    echo "📁 目标目录不存在，正在自动创建: $TARGET_DIR"
    mkdir -p "$TARGET_DIR"
fi

# 确定可用 Python 解释器
PYTHON_BIN="python3"
if [ -x "$REPO_ROOT/.venv313/bin/python" ]; then
    PYTHON_BIN="$REPO_ROOT/.venv313/bin/python"
fi

# 校验写权限 (涵盖只读挂载与无写权限位目录，防范 root 容器环境下的 DAC 权限穿透)
if [ ! -w "$TARGET_DIR" ] || ! "$PYTHON_BIN" -c "import os, sys; sys.exit(0 if (os.stat(sys.argv[1]).st_mode & 0o222) != 0 else 1)" "$TARGET_DIR" 2>/dev/null; then
    echo "❌ 错误: 目标目录无写入权限: $TARGET_DIR" >&2
    exit 1
fi

# 1. 镜像同步全部前端文件
if command -v rsync >/dev/null 2>&1; then
    echo "🚀 正在使用 rsync 镜像同步前端文件..."
    rsync -av --delete \
        --exclude='.DS_Store' \
        --exclude='*.bak*' \
        --exclude='__pycache__' \
        "$SRC_DIR/" "$TARGET_DIR/"
else
    echo "⚠️ 未安装 rsync，使用 Python 引擎安全同步前端文件..."
    "$PYTHON_BIN" -c "
import shutil
from pathlib import Path
src = Path('$SRC_DIR')
dst = Path('$TARGET_DIR')
dst.mkdir(parents=True, exist_ok=True)
for item in dst.iterdir():
    if item.name.startswith('.git') or '.bak' in item.name:
        continue
    if item.is_dir():
        shutil.rmtree(item)
    else:
        item.unlink()
for item in src.iterdir():
    if item.name in ('.DS_Store', '__pycache__') or '.bak' in item.name:
        continue
    target = dst / item.name
    if item.is_dir():
        shutil.copytree(item, target)
    else:
        shutil.copy2(item, target)
"
fi

# 2. 执行静态资源指纹计算与 Cache-Busting 注入
echo "🔨 正在计算 SHA-256 并注入 Cache-Busting 标识与构建版本 ($LOCAL_SHA)..."
"$PYTHON_BIN" "$SCRIPT_DIR/build_web_assets.py" "$TARGET_DIR" "$LOCAL_SHA"

# 3. 校验发布完整性
echo "🔍 正在进行发布完整性自检..."
if [ ! -f "$TARGET_DIR/index.html" ]; then
    echo "❌ 关键错误: $TARGET_DIR/index.html 缺失" >&2
    exit 1
fi

for req in main.js style.css modules components; do
    if [ ! -e "$TARGET_DIR/$req" ]; then
        echo "❌ 关键错误: $TARGET_DIR/$req 缺失" >&2
        exit 1
    fi
done

if ! grep -q "style.css?v=" "$TARGET_DIR/index.html" || ! grep -q "main.js?v=" "$TARGET_DIR/index.html"; then
    echo "❌ 关键错误: $TARGET_DIR/index.html 未检测到指纹注入" >&2
    exit 1
fi

if ! grep -q "$LOCAL_SHA" "$TARGET_DIR/index.html"; then
    echo "❌ 关键错误: $TARGET_DIR/index.html 未包含构建版本标识 $LOCAL_SHA" >&2
    exit 1
fi

echo "✅ [Local] 前端静态资源同步与指纹注入完成: $TARGET_DIR (Build: $LOCAL_SHA)"

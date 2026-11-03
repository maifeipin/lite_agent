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

# 处理 --remote 模式 (从本地向远端主机同步并执行远程构建)
if [ "${1:-}" = "--remote" ]; then
    REMOTE_HOST="${2:-vps1}"
    REMOTE_AGENT_DIR="/home/liteagent/lite_agent"
    REMOTE_TARGET="/var/www/dashboard/dist"
    echo "🌐 [Remote] 开始向 $REMOTE_HOST 远端同步静态资源..."

    # 1. 同步前端代码到远端 lite_agent 仓库的 web_dashboard 目录
    rsync -avz --delete \
        --exclude='.DS_Store' \
        --exclude='*.bak*' \
        --exclude='__pycache__' \
        "$SRC_DIR/" "$REMOTE_HOST:$REMOTE_AGENT_DIR/web_dashboard/"

    # 2. 在远端执行目标目录镜像同步与指纹构建
    ssh "$REMOTE_HOST" "su - liteagent -c '
        set -euo pipefail
        SRC=\"$REMOTE_AGENT_DIR/web_dashboard\"
        TARGET=\"$REMOTE_TARGET\"
        echo \"📦 [Remote VPS] 正在同步到 Nginx 静态目录: \$TARGET ...\"
        mkdir -p \"\$TARGET\"
        rsync -av --delete \
            --exclude=\".DS_Store\" \
            --exclude=\"*.bak*\" \
            \"\$SRC/\" \"\$TARGET/\"

        echo \"🔨 [Remote VPS] 执行静态资源 Cache-Busting 指纹注入...\"
        PYTHON_BIN=\"$REMOTE_AGENT_DIR/.venv313/bin/python\"
        if [ ! -x \"\$PYTHON_BIN\" ]; then
            PYTHON_BIN=\"python3\"
        fi
        \"\$PYTHON_BIN\" \"$REMOTE_AGENT_DIR/scripts/build_web_assets.py\" \"\$TARGET\"

        echo \"🔍 [Remote VPS] 静态资源完整性自检...\"
        test -f \"\$TARGET/index.html\"
        test -f \"\$TARGET/main.js\"
        test -f \"\$TARGET/style.css\"
        test -d \"\$TARGET/modules\"
        test -d \"\$TARGET/components\"
        grep -q \"style.css?v=\" \"\$TARGET/index.html\"
        grep -q \"main.js?v=\" \"\$TARGET/index.html\"
        echo \"✅ [Remote VPS] 远端静态资源发布成功！\"
    '"
    echo "🎉 [Remote] $REMOTE_HOST 远端前端发布全部完成！"
    exit 0
fi

# 本地发布模式
TARGET_DIR="${1:-/var/www/dashboard/dist}"
echo "📦 [Local] 开始同步前端静态资源: $SRC_DIR -> $TARGET_DIR"

if [ ! -d "$TARGET_DIR" ]; then
    echo "📁 目标目录不存在，正在自动创建: $TARGET_DIR"
    mkdir -p "$TARGET_DIR"
fi

if [ ! -w "$TARGET_DIR" ]; then
    echo "❌ 错误: 目标目录无写入权限: $TARGET_DIR" >&2
    exit 1
fi

# 1. 使用 rsync 增量同步全部前端文件
echo "🚀 正在使用 rsync 镜像同步前端文件..."
rsync -av --delete \
    --exclude='.DS_Store' \
    --exclude='*.bak*' \
    --exclude='__pycache__' \
    "$SRC_DIR/" "$TARGET_DIR/"

# 2. 执行静态资源指纹计算与 Cache-Busting 注入
echo "🔨 正在计算 SHA-256 并注入 Cache-Busting 标识与构建版本..."
PYTHON_BIN="python3"
if [ -x "$REPO_ROOT/.venv313/bin/python" ]; then
    PYTHON_BIN="$REPO_ROOT/.venv313/bin/python"
fi

"$PYTHON_BIN" "$SCRIPT_DIR/build_web_assets.py" "$TARGET_DIR"

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

echo "✅ [Local] 前端静态资源同步与指纹注入完成: $TARGET_DIR"

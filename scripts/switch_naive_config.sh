#!/usr/bin/env bash
# ========================================================
# Helper script to safely switch NaiveProxy configuration
# Entry point for privileged actions: backup, replace, restart & rollback
# ========================================================
set -euo pipefail

NEW_CONFIG_TMP="$1"
CONFIG_DIR="/etc/proxy"
CONFIG_FILE="${CONFIG_DIR}/config.json"
BACKUP_FILE="${CONFIG_DIR}/config.json.bak"
STAGING_FILE="${CONFIG_DIR}/config.json.new"

if [ ! -f "$NEW_CONFIG_TMP" ]; then
    echo "ERROR: Temp config file $NEW_CONFIG_TMP does not exist."
    exit 1
fi

# 1. 验证临时 JSON 语法格式
if command -v jq &>/dev/null; then
    jq . "$NEW_CONFIG_TMP" >/dev/null || { echo "ERROR: Invalid JSON in $NEW_CONFIG_TMP"; exit 1; }
fi

# 2. 保证目标目录存在并备份原配置
mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ]; then
    cp -f "$CONFIG_FILE" "$BACKUP_FILE"
fi

# 3. 在同一文件系统下原子替换配置文件 (S-2)
cp -f "$NEW_CONFIG_TMP" "$STAGING_FILE"
rm -f "$NEW_CONFIG_TMP"
mv -f "$STAGING_FILE" "$CONFIG_FILE"

# 4. 重启 naive 服务
systemctl restart naive.service

# 5. 5秒短轮询校验服务状态 (S-4)
IS_ACTIVE=0
for i in {1..5}; do
    if systemctl is-active --quiet naive.service; then
        IS_ACTIVE=1
        break
    fi
    sleep 1
done

# 6. 若校验失败，自动恢复备份并二次重启校验
if [ $IS_ACTIVE -eq 0 ]; then
    echo "ERROR: naive.service failed to activate after config update. Rolling back..."
    if [ -f "$BACKUP_FILE" ]; then
        cp -f "$BACKUP_FILE" "$CONFIG_FILE"
        systemctl restart naive.service
        for i in {1..5}; do
            if systemctl is-active --quiet naive.service; then
                echo "Rollback restored naive.service active state."
                break
            fi
            sleep 1
        done
    fi
    exit 1
fi

echo "SUCCESS: naive.service successfully restarted and active."
exit 0

#!/usr/bin/env bash
# 盯 weekly_run.log: 只在新里程碑出现时通知 + 每5min keep-alive, 命中 DONE 即退出。
# 用法: bash monitor_weekly.sh [log_path]   (默认 mac_work_dir/weekly_run.log)
SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
MAC_WORK=$(python3 "$SCRIPTS/paths.py" mac_work_dir "~/projects/rss_topic")
LOG="${1:-$MAC_WORK/weekly_run.log}"
last=""
last_ka=$(date +%s)
while true; do
  if [ -f "$LOG" ]; then
    cur=$(grep -E "^===== |saved model|-> [0-9]+ topics|named [0-9]+|upsert|miss=|DONE weekly|FAILED [^ ]" \
           "$LOG" 2>/dev/null | grep -v -i blake2 | tail -1)
    if [ -n "$cur" ] && [ "$cur" != "$last" ]; then
      echo "[$(date +%H:%M:%S)] $cur"
      last="$cur"; last_ka=$(date +%s)
    fi
    if grep -q "===== DONE weekly" "$LOG" 2>/dev/null; then
      echo "===== PIPELINE DONE ====="; break
    fi
    now=$(date +%s)
    if [ $((now - last_ka)) -ge 300 ]; then
      echo "[$(date +%H:%M:%S)] keep-alive (running, last: ${last:--})"
      last_ka=$now
    fi
  else
    echo "[$(date +%H:%M:%S)] waiting for log..."
    last_ka=$(date +%s)
  fi
  sleep 30
done

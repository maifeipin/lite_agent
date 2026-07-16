#!/usr/bin/env bash
# RSS2MeiliSearch + 热点 SOP 一键调度 (Mac 运行, 编排 Mac + vps1)
# 用法:
#   bash run_rss2meili.sh daily           # 每日: 近24h新文 -> transform 归类 -> 增量push -> 热点检测 (~5min)
#   bash run_rss2meili.sh weekly           # 每周: 全量重聚类+存模型 -> 命名 -> push -> 热点+话题diff (~35min)
#   bash run_rss2meili.sh weekly --dedup   # weekly 末尾追加去重
# 前提: Mac ~/projects/rss_topic/bertopic_env + 缓存 embeddings.npy; ssh vps1 配好。
#       weekly 至少跑过一次, daily 才有 topic_model/ 可 load+transform。
set -eo pipefail

MODE="${1:-daily}"; DEDUP=0
[ "${2:-}" = "--dedup" ] && DEDUP=1
case "$MODE" in daily|weekly) ;; *) echo "usage: $0 daily|weekly [--dedup]"; exit 1;; esac

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"          # scripts/rss_topic/ (committed 源)
MAC_WORK="$HOME/projects/rss_topic"               # Mac 数据/venv 目录
VPS_WORK="/home/liteagent/rss_topic_work"          # vps1 运行/数据目录
VPS_TMP="/tmp"
ENV="/home/liteagent/lite_agent/.env"
ENVV='export MEILI_MASTER_KEY="$(grep ^MEILI_MASTER_KEY= '"$ENV"'|cut -d= -f2-)"; export RSSDB_URI="$(grep ^RSSDB_URI= '"$ENV"'|cut -d= -f2-)"; export DEEPSEEK_API_KEY="$(grep ^DEEPSEEK_API_KEY= '"$ENV"'|cut -d= -f2-)"; export API_AUTH_TOKEN="$(grep ^API_AUTH_TOKEN= '"$ENV"'|cut -d= -f2-)"'

echo "===== RSS pipeline: $MODE ($(date)) ====="

echo "===== 0. 部署 vps1 脚本 -> $VPS_WORK ====="
ssh vps1 "mkdir -p $VPS_WORK/history"
scp "$SCRIPTS"/step2a_export.py "$SCRIPTS"/step2b_mongo_enrich.py "$SCRIPTS"/step2c_backfill.py \
    "$SCRIPTS"/name_topics.py "$SCRIPTS"/push_topics_v2.py "$SCRIPTS"/hotspot.py \
    "$SCRIPTS"/topic_diff.py "$SCRIPTS"/dedup_meili.py vps1:"$VPS_WORK"/

DAYS=0; [ "$MODE" = "daily" ] && DAYS=1
echo "===== 1. vps1: 导出 Meili (--days $DAYS) + Mongo 取 excerpt ====="
ssh vps1 "$ENVV; cd $VPS_WORK && python3 step2a_export.py --days $DAYS && python3 step2b_mongo_enrich.py && python3 step2c_backfill.py"

echo "===== 2. scp rss_all.jsonl vps1 -> Mac ====="
scp vps1:"$VPS_WORK/rss_all.jsonl" "$MAC_WORK/rss_all.jsonl"

echo "===== 3. Mac: classify_cluster --mode $MODE ====="
cd "$MAC_WORK" && { source bertopic_env/bin/activate 2>/dev/null || true; } && python "$SCRIPTS/classify_cluster.py" --mode "$MODE"

echo "===== 4. scp topic_labels.json Mac -> vps1:/tmp ====="
scp "$MAC_WORK/topic_labels.json" vps1:"$VPS_TMP/topic_labels.json"

echo "===== 5. vps1: DeepSeek 命名 (daily 走 cache, weekly 才真命名) ====="
ssh vps1 "$ENVV; python3 $VPS_WORK/name_topics.py"

echo "===== 6. vps1: 推送 Meili (增量/全量由 topic_labels 决定) ====="
ssh vps1 "$ENVV; python3 $VPS_WORK/push_topics_v2.py"

echo "===== 7. vps1: 热点检测 ====="
ssh vps1 "$ENVV; python3 $VPS_WORK/hotspot.py" || echo "  (hotspot skipped)"

if [ "$MODE" = "weekly" ]; then
  echo "===== 8. vps1: 话题变化 diff (weekly) ====="
  ssh vps1 "$ENVV; python3 $VPS_WORK/topic_diff.py" || echo "  (diff skipped)"
fi
if [ "$DEDUP" = "1" ]; then
  echo "===== 9. vps1: 去重 ====="
  ssh vps1 "$ENVV; python3 $VPS_WORK/dedup_meili.py --apply"
fi

echo "===== DONE $MODE $(date) ====="

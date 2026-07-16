#!/usr/bin/env python3
"""Stage 2a: 从 Meilisearch rss 分页导出文档(id/title/source/link/published/date)。
运行于 vps1。默认全量; --days N 只导最近 N 天(按 date 字段, daily 用)。
按 id 去重应对索引实时增长。输出 /home/liteagent/rss_topic_work/meili_docs.jsonl。
MEILI_MASTER_KEY 从环境变量读。
"""
import os, json, urllib.request, argparse, time
from datetime import datetime, timedelta
from paths import cfg

KEY = os.environ["MEILI_MASTER_KEY"]
URL = cfg("meili_url", "http://127.0.0.1:7700")
OUT_DIR = cfg("vps_work_dir", "/home/liteagent/rss_topic_work")
OUT = OUT_DIR + "/meili_docs.jsonl"
os.makedirs(OUT_DIR, exist_ok=True)

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=0, help="只导最近 N 天(0=全量)")
args = ap.parse_args()

FIELDS = ["id", "title", "source", "link", "published", "date"]
BATCH = 1000
threshold = 0
if args.days > 0:
    threshold = int((datetime.now() - timedelta(days=args.days)).timestamp())
    print("--days {}: date >= {} ({})".format(args.days, threshold, datetime.fromtimestamp(threshold)), flush=True)


def fetch(offset, limit):
    url = URL + "/indexes/rss/documents?limit=" + str(limit) + "&offset=" + str(offset)
    r = urllib.request.Request(url, headers={"Authorization": "Bearer " + KEY})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode())


meta = fetch(0, 1)
total = meta["total"]
print("Meili rss total={}".format(total), flush=True)

seen = set()
written = 0
skipped_old = 0
offset = 0
with open(OUT, "w", encoding="utf-8") as f:
    while offset < total:
        res = fetch(offset, BATCH)
        results = res.get("results", []) if isinstance(res, dict) else res
        if not results:
            break
        for d in results:
            did = d.get("id")
            if not did or did in seen:
                continue
            # --days 过滤 (date 是 unix ts 字符串)
            if threshold:
                try:
                    dt = int(float(d.get("date") or 0))
                except Exception:
                    dt = 0
                if dt and dt < threshold:
                    skipped_old += 1
                    continue
            seen.add(did)
            rec = {k: (d.get(k, "") or "") for k in FIELDS}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
        offset += BATCH
        if offset % 50000 == 0 or offset >= total:
            print("  offset={} written={} unique={} (skipped_old={})".format(
                offset, written, len(seen), skipped_old), flush=True)
        if len(results) < BATCH:
            break

print("\nDONE: written={} unique_ids={} (skipped_old={})".format(written, len(seen), skipped_old), flush=True)
print("file: {:.1f} MB".format(os.path.getsize(OUT) / 1024 / 1024), flush=True)

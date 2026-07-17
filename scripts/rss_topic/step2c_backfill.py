#!/usr/bin/env python3
"""Step 2c:回补现有 Meili 数据的正确 content/published/date(运行于 vps1)。
读 meili_docs.jsonl 的 id,按 ObjectId gen_month±1 回 Mongo 取 excerpt(全文)+pubdate,
strip HTML -> content,parse pubdate -> date,写出 meili_backfill.jsonl: {id,content,published,date}。
凭证从环境变量读。供 push 脚本与 topics 一起 upsert。"""
import os
import re
import html
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import email.utils as email_utils
from bson import ObjectId
from pymongo import MongoClient
from paths import cfg

RSSDB_URI = os.environ["RSSDB_URI"]
W = cfg("vps_work_dir", "/home/liteagent/rss_topic_work")
IN = W + "/meili_docs.jsonl"
OUT = W + "/meili_backfill.jsonl"

mc = MongoClient(RSSDB_URI, serverSelectionTimeoutMS=10000)
db = mc["rsslite"]
existing = set(db.list_collection_names())
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def strip_html(raw):
    if not raw:
        return ""
    t = TAG.sub(" ", str(raw))
    t = html.unescape(t)
    return WS.sub(" ", t).strip()


def parse_ts(v, doc_id=""):
    """naive 按 UTC; 启发式: pubdate(按UTC) 晚于入库时间 5min 以上 -> naive-CST(Now兜底), 减 8h。"""
    if not v:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    insert_time = None
    try:
        insert_time = ObjectId(doc_id).generation_time.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # naive 串按 UTC, 避免 .timestamp() 按本地 CST 误算
        if insert_time and dt - insert_time > timedelta(minutes=5):
            dt = dt - timedelta(hours=8)  # naive-CST(Now兜底)被当UTC -> 减8h
        return int(dt.timestamp())
    except (ValueError, TypeError):
        pass
    try:
        dt = email_utils.parsedate_to_datetime(s)
        if dt:
            return int(dt.timestamp())
    except Exception:
        pass
    return 0


def month_str(dt, d):
    idx = dt.year * 12 + (dt.month - 1) + d
    return "{:04d}{:02d}".format(idx // 12, idx % 12 + 1)


# 1. 按 gen_month 分组
by_month = defaultdict(list)
with open(IN, encoding="utf-8") as f:
    for line in f:
        did = json.loads(line)["id"]
        try:
            oid = ObjectId(did)
        except Exception:
            continue
        by_month[oid.generation_time.strftime("%Y%m")].append((did, oid))
print("loaded {} ids, {} months".format(sum(len(v) for v in by_month.values()), len(by_month)))

# 2. 回 Mongo 取 excerpt+pubdate
backfill = {}  # id -> (content, published, date)
for gm, items in sorted(by_month.items()):
    dt = datetime.strptime(gm, "%Y%m")
    cands = ["FeedItem_" + c for c in (month_str(dt, -1), month_str(dt, 0), month_str(dt, 1)) if "FeedItem_" + c in existing]
    oids = [i[1] for i in items]
    id_set = set(i[0] for i in items)
    for col in cands:
        for i in range(0, len(oids), 5000):
            for doc in db[col].find({"_id": {"$in": oids[i:i + 5000]}}, {"excerpt": 1, "pubdate": 1, "published": 1}):
                did = str(doc["_id"])
                if did in id_set and did not in backfill:
                    pub = doc.get("pubdate", "") or doc.get("published", "") or ""
                    backfill[did] = (
                        strip_html(doc.get("excerpt") or doc.get("content") or doc.get("description") or ""),
                        pub,
                        parse_ts(pub, did),
                    )
    print("  {} docs={} backfilled={}".format(gm, len(items), sum(1 for i in items if i[0] in backfill)))

# 3. 写出
with open(OUT, "w", encoding="utf-8") as f:
    for did, (content, pub, date) in backfill.items():
        f.write(json.dumps({"id": did, "content": content, "published": pub, "date": date}, ensure_ascii=False) + "\n")

print("\nDONE backfill records={} size={:.1f}MB".format(
    len(backfill), os.path.getsize(OUT) / 1024 / 1024))
clens = [len(v[0]) for v in backfill.values()]
print("content_len: nonempty={} ({:.1f}%) mean={}".format(
    sum(1 for c in clens if c), 100 * sum(1 for c in clens if c) / len(clens), int(sum(clens) / len(clens))))

#!/usr/bin/env python3
"""Step 2b:MongoDB 按 id 取 excerpt(真正正文),strip HTML,合并 Meili 文档 -> rss_all.jsonl。
运行于 vps1。读 meili_docs.jsonl,按 ObjectId gen_month 分组,查 gen_month±1 候选集合($in 批量),
未命中者回退扫全部集合。凭证从环境变量读。
输出:/home/liteagent/rss_topic_work/rss_all.jsonl  {id,title,source,excerpt}"""
import json
import os
import re
import html
from collections import defaultdict
from datetime import datetime
from bson import ObjectId
from pymongo import MongoClient
from paths import cfg

RSSDB_URI = os.environ["RSSDB_URI"]
W = cfg("vps_work_dir", "/home/liteagent/rss_topic_work")
IN = W + "/meili_docs.jsonl"
OUT = W + "/rss_all.jsonl"
TRUNC = 1500

mc = MongoClient(RSSDB_URI, serverSelectionTimeoutMS=10000)
db = mc["rsslite"]
existing_cols = set(db.list_collection_names())
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def clean_excerpt(raw):
    if not raw:
        return ""
    t = TAG.sub(" ", raw)
    t = html.unescape(t)
    t = WS.sub(" ", t).strip()
    return t[:TRUNC]


def month_str(dt, delta):
    idx = dt.year * 12 + (dt.month - 1) + delta
    return "{:04d}{:02d}".format(idx // 12, idx % 12 + 1)


# 1. 读 meili_docs.jsonl,按 gen_month 分组,同时缓存 id->(title,source)
print("reading meili_docs.jsonl ...")
by_month = defaultdict(list)   # gen_month -> [(id, oid)]
meta = {}                      # id -> (title, source)
n = 0
with open(IN, encoding="utf-8") as f:
    for line in f:
        d = json.loads(line)
        did = d["id"]
        try:
            oid = ObjectId(did)
        except Exception:
            continue
        gm = oid.generation_time.strftime("%Y%m")
        by_month[gm].append((did, oid))
        meta[did] = (d.get("title", "") or "", d.get("source", "") or "")
        n += 1
print("loaded {} docs, {} gen_months".format(n, len(by_month)))

# 2. 按 gen_month 查候选集合(gen_month±1)
excerpt_map = {}   # id -> excerpt(plain)
not_found = []
for gm, items in sorted(by_month.items()):
    dt = datetime.strptime(gm, "%Y%m")
    cands = [c for c in (month_str(dt, -1), month_str(dt, 0), month_str(dt, 1))
             if "FeedItem_" + c in existing_cols]
    ids = [i[0] for i in items]
    oids = [i[1] for i in items]
    id_set = set(ids)
    found_here = 0
    for c in cands:
        col = db["FeedItem_" + c]
        # 分批 $in 防止单次过大
        for i in range(0, len(oids), 5000):
            chunk = oids[i:i + 5000]
            for doc in col.find({"_id": {"$in": chunk}}, {"excerpt": 1}):
                did = str(doc["_id"])
                if did in id_set and did not in excerpt_map:
                    excerpt_map[did] = clean_excerpt(doc.get("excerpt", ""))
                    found_here += 1
    missing = id_set - set(excerpt_map.keys())
    for mid in missing:
        not_found.append((mid, ObjectId(mid)))
    print("  gen_month={} docs={} found_in_candidates={} missing={}".format(
        gm, len(items), found_here, len(missing)))

print("\nfound in candidates: {} / {}".format(len(excerpt_map), n))
print("not_found (will scan all): {}".format(len(not_found)))

# 3. 回退:未命中者扫全部集合
if not_found:
    all_cols = sorted(c for c in existing_cols if c.startswith("FeedItem"))
    nf_ids = {mid for mid, _ in not_found}
    nf_oids_by_col = defaultdict(list)
    # 不知在哪,对每个集合 $in 全部未命中 oid
    for mid, oid in not_found:
        for col in all_cols:
            nf_oids_by_col[col].append(oid)
    recovered = 0
    for col in all_cols:
        oids = nf_oids_by_col[col]
        if not oids:
            continue
        for i in range(0, len(oids), 5000):
            chunk = oids[i:i + 5000]
            for doc in db[col].find({"_id": {"$in": chunk}}, {"excerpt": 1}):
                did = str(doc["_id"])
                if did in nf_ids and did not in excerpt_map:
                    excerpt_map[did] = clean_excerpt(doc.get("excerpt", ""))
                    nf_ids.discard(did)
                    recovered += 1
        if not nf_ids:
            break
    print("recovered via full scan: {}".format(recovered))
    print("truly not in mongo: {}".format(len(nf_ids)))

# 4. 合并写出 rss_all.jsonl
print("\nwriting rss_all.jsonl ...")
empty_excerpt = 0
written = 0
with open(OUT, "w", encoding="utf-8") as f:
    for did, (title, source) in meta.items():
        ex = excerpt_map.get(did, "")
        if not ex:
            empty_excerpt += 1
        f.write(json.dumps({"id": did, "title": title, "source": source, "excerpt": ex},
                           ensure_ascii=False) + "\n")
        written += 1

print("\nDONE written={}".format(written))
print("with_excerpt={} (empty_excerpt={} {:.1f}%)".format(
    written - empty_excerpt, empty_excerpt, 100 * empty_excerpt / written))
print("file size: {:.1f} MB".format(os.path.getsize(OUT) / 1024 / 1024))

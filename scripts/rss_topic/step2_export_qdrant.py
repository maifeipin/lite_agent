#!/usr/bin/env python3
"""Stage 2: 从 Qdrant 向量数据库直接导出文档(id/title/source/link/published/excerpt + vector)。
运行于 vps1 / 本地。支持 --days N 过滤。
替代原有的 step2a_export.py (Meilisearch) + step2b_mongo_enrich.py (MongoDB) 组合链路。
直接输出 rss_all.jsonl 与 (可选) embeddings.npy。
"""
import os, json, urllib.request, argparse, time
import numpy as np
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from paths import cfg

QDRANT_URL = cfg("qdrant_url", "http://127.0.0.1:6333")
OUT_DIR = cfg("vps_work_dir", "/home/liteagent/rss_topic_work")
OUT_JSONL = OUT_DIR + "/rss_all.jsonl"
OUT_EMB = OUT_DIR + "/embeddings.npy"
OUT_IDS = OUT_DIR + "/doc_ids.json"
os.makedirs(OUT_DIR, exist_ok=True)

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=0, help="只导最近 N 天(0=全量)")
ap.add_argument("--with-vectors", action="store_true", help="同时导出 embeddings.npy 和 doc_ids.json")
args = ap.parse_args()

BATCH = 1000
threshold_dt = None
if args.days > 0:
    threshold_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"--days {args.days}: PubDate >= {threshold_dt.isoformat()}", flush=True)


def parse_mongo_id_from_guid(guid_str):
    """从 16-byte GUID 转回 24-char Mongo ObjectId 16进制字符串"""
    try:
        import uuid
        u = uuid.UUID(guid_str)
        return u.bytes[:12].hex()
    except Exception:
        return guid_str


def get_source_host(link_url):
    """从 URL 提取域名作为来源 fallback"""
    if not link_url:
        return ""
    try:
        host = urlparse(link_url).netloc
        return host.replace("www.", "")
    except Exception:
        return link_url


def scroll_points(offset_point_id=None):
    url = f"{QDRANT_URL}/collections/feed_items/points/scroll"
    payload_data = {
        "limit": BATCH,
        "with_payload": True,
        "with_vector": args.with_vectors
    }
    if offset_point_id:
        payload_data["offset"] = offset_point_id

    req = urllib.request.Request(
        url,
        data=json.dumps(payload_data).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        return res.get("result", {})


print("Start scrolling Qdrant feed_items collection...", flush=True)

seen = set()
written = 0
skipped_old = 0
skipped_no_vec = 0
next_offset = None
vectors_list = []
doc_ids_list = []

with open(OUT_JSONL, "w", encoding="utf-8") as f:
    while True:
        res = scroll_points(next_offset)
        points = res.get("points", [])
        next_offset = res.get("next_page_offset")

        if not points:
            break

        for p in points:
            point_id = p.get("id")
            doc_id = parse_mongo_id_from_guid(point_id)
            if not doc_id or doc_id in seen:
                continue

            # 校验 1: 如果请求带向量导出，但点缺少向量，则跳过，保证 jsonl 与 embeddings.npy 严格 1:1 对齐
            if args.with_vectors:
                vec = p.get("vector")
                if vec is None:
                    skipped_no_vec += 1
                    continue

            payload = p.get("payload", {})
            title = payload.get("Title", "") or ""
            link = payload.get("Link", "") or ""
            excerpt = payload.get("Excerpt", "") or ""
            pub_date_str = payload.get("PubDate", "") or ""
            source = payload.get("Source", "") or get_source_host(link)

            # 校验 2: 时区感知日期过滤
            if threshold_dt and pub_date_str:
                try:
                    dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = dt.astimezone(timezone.utc)
                    if dt < threshold_dt:
                        skipped_old += 1
                        continue
                except Exception:
                    pass

            seen.add(doc_id)
            rss_node_id_val = payload.get("RssNodeId", "")
            try:
                rss_node_id_val = int(rss_node_id_val)
            except Exception:
                pass

            rec = {
                "id": doc_id,
                "title": title,
                "source": source,
                "link": link,
                "excerpt": excerpt,
                "published": pub_date_str,
                "rssNodeId": rss_node_id_val
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1

            if args.with_vectors:
                vectors_list.append(p["vector"])
                doc_ids_list.append(doc_id)

        print(f"  scrolled points={len(seen)}, written={written}, skipped_old={skipped_old}, skipped_no_vec={skipped_no_vec}", flush=True)

        if not next_offset:
            break

if args.with_vectors and vectors_list:
    print(f"Saving {len(vectors_list)} pre-calculated vectors to {OUT_EMB}...", flush=True)
    np.save(OUT_EMB, np.array(vectors_list, dtype=np.float32))
    with open(OUT_IDS, "w", encoding="utf-8") as f_ids:
        json.dump(doc_ids_list, f_ids)

print(f"\nDONE: written={written} unique_ids={len(seen)} (skipped_old={skipped_old}, skipped_no_vec={skipped_no_vec})", flush=True)
print(f"file: {os.path.getsize(OUT_JSONL) / 1024 / 1024:.1f} MB", flush=True)

#!/usr/bin/env python3
"""一次性去重 Meili rss 索引: 按 link 保留最老(ObjectId 字典序最小≈首抓)的一份,
删多余副本。运行于 vps1。
默认 dry-run(只统计不删); 加 --apply 才真删。
MEILI_MASTER_KEY 从环境变量读。
用法:
  ssh vps1 'export MEILI_MASTER_KEY="$(grep ^MEILI_MASTER_KEY= /home/liteagent/lite_agent/.env|cut -d= -f2-)"; python3 /tmp/dedup_meili.py'          # dry-run
  ssh vps1 'export MEILI_MASTER_KEY="$(grep ^MEILI_MASTER_KEY= /home/liteagent/lite_agent/.env|cut -d= -f2-)"; python3 /tmp/dedup_meili.py --apply'  # 真删
"""
import os, json, urllib.request, sys, time
from collections import defaultdict, Counter
from paths import cfg

KEY = os.environ["MEILI_MASTER_KEY"]
URL = cfg("meili_url", "http://127.0.0.1:7700")
INDEX = "rss"
APPLY = "--apply" in sys.argv
FETCH = 1000
DEL_BATCH = 2000


def req(path, method="GET", data=None, timeout=180):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(URL + path, data=body, method=method,
                               headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode() or "{}")


# 1. 取全量 (id, link) -- /documents 端点无 maxTotalHits 上限
print("fetching all docs (id, link) via /documents ...", flush=True)
link_ids = defaultdict(list)
n = 0
offset = 0
while True:
    st, resp = req("/indexes/%s/documents?limit=%d&offset=%d&fields=id,link" % (INDEX, FETCH, offset))
    docs = resp.get("results", []) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
    if not docs:
        break
    for d in docs:
        link_ids[d.get("link") or ""].append(d["id"])
        n += 1
    offset += FETCH
    if n % 30000 == 0:
        print("  fetched %d ..." % n, flush=True)
    if len(docs) < FETCH:
        break
print("total docs: %d | distinct links: %d" % (n, len(link_ids)), flush=True)

# 2. 找重复: 保留 ObjectId 字典序最小(最老), 删其余
empty_link = len(link_ids.get("", []))
to_delete = []
dup_groups = 0
host_extra = Counter()
for link, ids in link_ids.items():
    if link == "":
        continue  # 空 link 不去重
    if len(ids) > 1:
        dup_groups += 1
        to_delete.extend(sorted(ids)[1:])  # 留最小, 删其余
        host = link.split("/")[2] if link.startswith("http") and len(link.split("/")) > 2 else "?"
        host_extra[host] += len(ids) - 1
print("empty-link docs (kept all): %d" % empty_link, flush=True)
print("dup link groups: %d | to delete: %d | will keep: %d" % (
    dup_groups, len(to_delete), n - len(to_delete)), flush=True)
print("extra dup docs by host (top 8):", flush=True)
for h, c in host_extra.most_common(8):
    print("  %7d  %s" % (c, h), flush=True)

if not APPLY:
    print("\n[DRY RUN] 未删除任何文档。确认后用 --apply 真删。", flush=True)
    sys.exit(0)

# 3. 批量删除
print("\n=== APPLY: deleting %d docs (batch %d) ===" % (len(to_delete), DEL_BATCH), flush=True)
total = 0
for i in range(0, len(to_delete), DEL_BATCH):
    batch = to_delete[i:i + DEL_BATCH]
    st, _ = req("/indexes/%s/documents/delete-batch" % INDEX, "POST", batch)
    if st == 202:
        total += len(batch)
        print("  enqueued %d / %d" % (total, len(to_delete)), flush=True)
    else:
        print("  FAIL at %d: status %d" % (total, st), flush=True)
print("enqueued delete of %d docs" % total, flush=True)

# 4. 等重索引
print("waiting for indexing ...", flush=True)
for _ in range(200):
    time.sleep(3)
    st, stats = req("/indexes/%s/stats" % INDEX)
    if not stats.get("isIndexing"):
        break
st, stats = req("/indexes/%s/stats" % INDEX)
print("indexing=%s numberOfDocuments=%s" % (stats.get("isIndexing"), stats.get("numberOfDocuments")), flush=True)

# 5. 去重后 topics facet 预览
st, fd = req("/indexes/%s/search" % INDEX, "POST", {"q": "", "limit": 0, "facets": ["topics"]})
topics = fd.get("facetDistribution", {}).get("topics", {})
print("\n=== topics facet after dedup (top 8) ===", flush=True)
for k, c in sorted(topics.items(), key=lambda x: -x[1])[:8]:
    print("  %7d  %s" % (c, k), flush=True)

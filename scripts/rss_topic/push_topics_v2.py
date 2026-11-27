#!/usr/bin/env python3
"""Step 5 (分类版): 推送 category + topics + 回补 content 到 Meilisearch (vps1)。
合并:
  - /tmp/topic_labels.json   (doc_category: {id:cat}, doc_topic_name: {id:name})
  - /home/liteagent/rss_topic_work/meili_backfill.jsonl (id -> content/published/date)
1) PATCH filterableAttributes 加 category + topics
2) 批量 upsert {id, category, topics:[name], content, published, date}
MEILI_MASTER_KEY 从环境变量读。用法:
  scp topic_labels.json vps1:/tmp/
  ssh vps1 'export MEILI_MASTER_KEY="$(grep ^MEILI_MASTER_KEY= /home/liteagent/lite_agent/.env|cut -d= -f2-)"; python3 /tmp/push_topics_v2.py'
"""
import os, json, urllib.request, time
from paths import cfg

KEY = os.environ["MEILI_MASTER_KEY"]
URL = cfg("meili_url", "http://127.0.0.1:7700")
TL = cfg("vps_tmp_dir", "/tmp") + "/topic_labels.json"
BF = cfg("vps_work_dir", "/home/liteagent/rss_topic_work") + "/meili_backfill.jsonl"
BATCH = 200


def req(path, method="GET", data=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(URL + path, data=body, method=method,
                               headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return resp.status, json.loads(resp.read().decode() or "{}")


# 1. PATCH filterable: 加 category + topics (幂等)
st, cur = req("/indexes/rss/settings", "GET")
fa = cur.get("filterableAttributes", [])
added = [f for f in ("category", "topics") if f not in fa]
if added:
    fa = fa + added
    st, _ = req("/indexes/rss/settings", "PATCH", {"filterableAttributes": fa})
    print("PATCH filterable +{} -> {}".format(added, fa), flush=True)
else:
    print("category+topics already filterable: {}".format(fa), flush=True)
time.sleep(2)

# 2. 读
data = json.load(open(TL, encoding="utf-8"))
doc_cat = data.get("doc_category", {})
doc_name = data.get("doc_topic_name", {})
print("docs_with_category={} docs_with_topic={}".format(len(doc_cat), len(doc_name)), flush=True)

backfill = {}
with open(BF, encoding="utf-8") as f:
    for line in f:
        d = json.loads(line)
        backfill[d["id"]] = d
print("backfill records={}".format(len(backfill)), flush=True)

# 3. upsert
WAIT_TIMEOUT = 600
batch = []
total = 0
miss_cat = miss_name = miss_bf = 0
last_task_uid = None


def flush_batch(items):
    global total, last_task_uid
    st, resp = req("/indexes/rss/documents?primaryKey=id", "PUT", items)
    if st != 202:
        print("  FAIL at {}: HTTP {} {}".format(total, st, resp), flush=True)
        raise SystemExit(1)
    task_uid = resp.get("taskUid")
    if task_uid is None:
        task_uid = resp.get("uid")
    if task_uid is not None:
        last_task_uid = task_uid
    total += len(items)
    if total % 5000 == 0:
        print("  upserted {}...".format(total), flush=True)


def wait_for_task(task_uid, timeout=WAIT_TIMEOUT):
    deadline = time.monotonic() + timeout
    last_status = None
    while True:
        st, task = req("/tasks/{}".format(task_uid), "GET")
        last_status = task.get("status")
        if last_status == "succeeded":
            print("  task {} succeeded (duration={})".format(task_uid, task.get("duration")), flush=True)
            return
        if last_status in ("failed", "canceled"):
            print("  ERROR: task {} {} -> {}".format(task_uid, last_status, task.get("error")), flush=True)
            raise SystemExit(1)
        if time.monotonic() >= deadline:
            print("  ERROR: task {} timed out after {}s (status={})".format(
                task_uid, timeout, last_status), flush=True)
            raise SystemExit(1)
        time.sleep(3)


ids = set(doc_cat) | set(doc_name)
for did in ids:
    cat = doc_cat.get(did, "")
    name = doc_name.get(did, "")
    if not cat:
        miss_cat += 1
    if not name:
        miss_name += 1
    bf = backfill.get(did, {})
    if not bf:
        miss_bf += 1
    doc = {
        "id": did,
        "category": cat or "其他",
        "topics": [name] if name else [],
    }
    bf_content = bf.get("content")
    if bf_content:
        doc["content"] = bf_content
    bf_published = bf.get("published")
    if bf_published:
        doc["published"] = bf_published
    try:
        bf_date = int(float(bf.get("date") or 0))
    except Exception:
        bf_date = 0
    if bf_date > 0:
        doc["date"] = bf_date
    batch.append(doc)
    if len(batch) >= BATCH:
        flush_batch(batch)
        batch = []
if batch:
    flush_batch(batch)

print("\nDONE upserted {} docs (miss_cat={} miss_name={} miss_bf={})".format(
    total, miss_cat, miss_name, miss_bf), flush=True)

# 等最后一批 taskUid 完成建索引
if last_task_uid is not None:
    print("waiting for taskUid={} (timeout={}s)...".format(last_task_uid, WAIT_TIMEOUT), flush=True)
    wait_for_task(last_task_uid, timeout=WAIT_TIMEOUT)

st, stats = req("/indexes/rss/stats")
print("indexing={} numberOfDocuments={}".format(stats.get("isIndexing"), stats.get("numberOfDocuments")), flush=True)
st, fd = req("/indexes/rss/search", "POST", {"q": "", "limit": 0, "facets": ["category", "topics"]})
cat_dist = fd.get("facetDistribution", {}).get("category", {})
print("\n=== category facet (preview) ===", flush=True)
for k, c in sorted(cat_dist.items(), key=lambda x: -x[1]):
    print("  {:>7}  {}".format(c, k), flush=True)

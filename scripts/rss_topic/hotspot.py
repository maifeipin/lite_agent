#!/usr/bin/env python3
"""Stage 5 热点检测 (vps1)。push 之后跑。
今日各 topic 计数 vs 过去 7 日均值, >3x 且日常>=3 触发; 新话题(历史<2天)今日>=20 也算。
history 存 /home/liteagent/rss_topic_work/history/{YYYYMMDD}_topic_counts.json。
命中则推送 lite-agent。MEILI_MASTER_KEY 从环境变量读。
"""
import os, json, urllib.request, time
from datetime import datetime, timedelta
from collections import defaultdict

KEY = os.environ["MEILI_MASTER_KEY"]
URL = "http://127.0.0.1:7700"
INDEX = "rss"
HISTORY = "/home/liteagent/rss_topic_work/history"
PUSH_URL = "http://127.0.0.1:8887/api/v1/chat"  # lite-agent (5000 是 RssAdapter; 路径 /api/v1/chat)
PUSH_TOKEN = os.environ.get("API_AUTH_TOKEN", "")


def req(path, method="GET", data=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(URL + path, data=body, method=method,
                               headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.status, json.loads(resp.read().decode() or "{}")


# 今日起点 (UTC 当天 00:00 的 unix ts; 用 date 字段过滤)
today_str = datetime.now().strftime("%Y%m%d")
today_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
print("today={} (date >= {})".format(today_str, today_start), flush=True)

# 今日各 topic 计数 (facet topics, 过滤今日文章)
st, d = req("/indexes/{}/search".format(INDEX), "POST",
            {"q": "", "limit": 0, "filter": "date >= {}".format(today_start), "facets": ["topics"]})
topic_counts = {k: v for k, v in d.get("facetDistribution", {}).get("topics", {}).items() if k and k != "未分类"}
print("today topics: {} (total today docs: {})".format(len(topic_counts), d.get("estimatedTotalHits")), flush=True)

os.makedirs(HISTORY, exist_ok=True)
today_file = "{}/{}_topic_counts.json".format(HISTORY, today_str)
json.dump(topic_counts, open(today_file, "w"), ensure_ascii=False, indent=2)

# 过去 7 日
history = defaultdict(list)
for i in range(1, 8):
    d_ = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
    f = "{}/{}_topic_counts.json".format(HISTORY, d_)
    if os.path.exists(f):
        for t, c in json.load(open(f)).items():
            history[t].append(c)

# 热点判定
hot = []
for topic, tc in topic_counts.items():
    past = history.get(topic, [])
    if len(past) < 2:
        if tc >= 20:
            hot.append((topic, tc, None, "新话题"))
        continue
    avg = sum(past) / len(past)
    if avg < 3:
        continue
    ratio = tc / avg
    if ratio > 3.0:
        hot.append((topic, tc, avg, "{:.1f}x".format(ratio)))

if not hot:
    print("无显著热点", flush=True)
    raise SystemExit(0)

hot.sort(key=lambda x: x[1], reverse=True)
lines = ["🔥 RSS 热点话题 ({})\n".format(today_str)]
for topic, c, avg, tag in hot[:5]:
    lines.append("  {}{}{}{}".format(topic, c, "篇（日常 {:.0f}，".format(avg) if avg else "篇（🆕 ", tag + "）" if avg else "）"))
msg = "\n".join(lines)
print(msg, flush=True)

try:
    urllib.request.urlopen(urllib.request.Request(PUSH_URL, data=json.dumps(
        {"session_id": "rss_hotspot_bot", "text": msg}).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + PUSH_TOKEN}), timeout=10)
    print("  -> pushed to lite-agent", flush=True)
except Exception as e:
    print("  push skip: {}".format(e), flush=True)

#!/usr/bin/env python3
"""Stage 5 热点检测 (vps1)。push 之后跑。
今日各 topic 计数 vs 过去 7 日均值, >3x 且日常>=3 触发; 新话题(历史<2天)今日>=20 也算。
history 存 /home/liteagent/rss_topic_work/history/{YYYYMMDD}_topic_counts.json。
命中则推送 lite-agent。MEILI_MASTER_KEY 从环境变量读。
"""
import os, json, urllib.request, time
from datetime import datetime, timedelta
from collections import defaultdict
from paths import cfg

KEY = os.environ["MEILI_MASTER_KEY"]
URL = cfg("meili_url", "http://127.0.0.1:7700")
INDEX = "rss"
HISTORY = cfg("vps_work_dir", "/home/liteagent/rss_topic_work") + "/history"
PUSH_URL = cfg("lite_agent_alert_url", "http://127.0.0.1:8887/api/v1/alert")
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
json.dump(topic_counts, open(today_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# 过去 7 日
history = defaultdict(list)
for i in range(1, 8):
    d_ = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
    f = "{}/{}_topic_counts.json".format(HISTORY, d_)
    if os.path.exists(f):
        for t, c in json.load(open(f, encoding="utf-8")).items():
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

def get_topic_docs(topic, limit=8):
    try:
        filter_expr = f'topics = "{topic}"'
        status, d = req(f"/indexes/{INDEX}/search", "POST", {
            "q": "",
            "filter": filter_expr,
            "limit": limit,
            "attributesToRetrieve": ["title", "content"]
        })
        return d.get("hits", [])
    except Exception as e:
        print("  Failed to fetch docs for topic {}: {}".format(topic, e), flush=True)
        return []

def generate_daily_brief(hot_topics):
    top_topics = hot_topics[:3]
    topics_details = []
    
    for idx, (topic, tc, avg, tag) in enumerate(top_topics):
        docs = get_topic_docs(topic, limit=8)
        articles_str = ""
        for d_idx, doc in enumerate(docs):
            title = doc.get("title", "无标题")
            content = doc.get("content", "无内容")[:300]
            articles_str += f"  - [{d_idx+1}] 标题: {title}\n    内容摘要: {content}\n\n"
        
        topics_details.append(
            f"### 话题 {idx+1}: {topic}\n"
            f"今日篇数: {tc} (日常: {avg or '🆕'}, 倍数/标签: {tag})\n"
            f"文章样本:\n{articles_str}"
        )
        
    topics_details_str = "\n---\n\n".join(topics_details)
    
    system_prompt = (
        "你是一个资深的行业资讯分析师。你需要根据给定的热点话题和相关的文章标题与摘要，生成一份专业、精炼的【每日热点话题简报】。\n"
        "简报需要输出为严格的 JSON 格式，必须包含且仅包含以下结构，不要有任何外层的 markdown 代码块包裹（只返回纯 JSON，最外层是一个大括号）：\n"
        "{\n"
        "  \"summary\": \"300字左右的今日热点总览，提炼各主要热点事件的背景与关联趋势。\",\n"
        "  \"topics\": [\n"
        "    {\n"
        "      \"topic\": \"话题名称\",\n"
        "      \"sentiment\": \"正/中/负\",\n"
        "      \"analysis\": \"100字左右的分析，说明该话题为何变热、核心要点是什么。\"\n"
        "    }\n"
        "  ]\n"
        "}"
    )
    
    user_prompt = f"以下是今日发现的 {len(top_topics)} 个热点话题的详细文章样本：\n\n{topics_details_str}\n\n请按要求生成 JSON 格式简报。"
    
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("  DEEPSEEK_API_KEY not found. Skipping daily brief generation.", flush=True)
        return None
        
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 1200,
        "temperature": 0.2,
        "response_format": {"type": "json_object"}
    }).encode()
    
    base_url = "https://api.deepseek.com/v1/chat/completions"
    
    for attempt in range(3):
        try:
            r = urllib.request.Request(base_url, data=body, method="POST",
                                       headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw_res = resp.read().decode("utf-8")
                res_data = json.loads(raw_res)
                content = res_data["choices"][0]["message"]["content"].strip()
                if content.startswith("```"):
                    lines = content.split("\n")
                    if lines[0].startswith("```json") or lines[0].startswith("```"):
                        content = "\n".join(lines[1:-1]).strip()
                return json.loads(content)
        except Exception as e:
            print(f"  Attempt {attempt+1} failed to call DeepSeek: {e}", flush=True)
            time.sleep(3)
    return None

if not hot:
    print("无显著热点", flush=True)
    # Persist empty brief
    empty_brief = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "summary": "今日未发现显著热点话题。",
        "topics": []
    }
    latest_brief_file = "{}/latest_brief.json".format(cfg("vps_work_dir", "/home/liteagent/rss_topic_work"))
    json.dump(empty_brief, open(latest_brief_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    raise SystemExit(0)

hot.sort(key=lambda x: x[1], reverse=True)

# Generate and persist brief
brief = generate_daily_brief(hot)
if brief:
    brief["date"] = datetime.now().strftime("%Y-%m-%d")
    
    # Save to history & latest
    brief_history_file = "{}/{}_brief.json".format(HISTORY, today_str)
    latest_brief_file = "{}/latest_brief.json".format(cfg("vps_work_dir", "/home/liteagent/rss_topic_work"))
    json.dump(brief, open(brief_history_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(brief, open(latest_brief_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("  -> Persisted brief to history & latest_brief.json", flush=True)
    
    # Build premium formatted alert push message
    topics_analysis = []
    for idx, t in enumerate(brief["topics"]):
        sentiment_emoji = "🟢 正" if t["sentiment"] == "正" else ("🔴 负" if t["sentiment"] == "负" else "⚪ 中")
        topics_analysis.append(
            "**{}. {}** (极性: {})\n> {}".format(idx+1, t["topic"], sentiment_emoji, t["analysis"])
        )
    topics_analysis_markdown = "\n\n".join(topics_analysis)
    
    msg = "🔥 **RSS 热点每日简报 ({})**\n\n【今日热点总览】\n{}\n\n【热点话题分析】\n{}".format(
        today_str, brief["summary"], topics_analysis_markdown
    )
else:
    # Fallback to simple topics list if LLM fails
    lines = ["🔥 RSS 热点话题 ({})\n".format(today_str)]
    for topic, c, avg, tag in hot[:5]:
        lines.append("  {}{}{}{}".format(topic, c, "篇（日常 {:.0f}，".format(avg) if avg else "篇（🆕 ", tag + "）"))
    msg = "\n".join(lines)

print(msg, flush=True)

try:
    urllib.request.urlopen(urllib.request.Request(
        PUSH_URL,
        data=json.dumps({"title": "🔥 RSS 热点每日简报 ({})".format(today_str),
                         "text": msg, "color": "red",
                         "dedup_key": "hotspot:{}".format(today_str)}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + PUSH_TOKEN}), timeout=10)
    print("  -> pushed to IM via /api/v1/alert", flush=True)
except Exception as e:
    print("  push skip: {}".format(e), flush=True)

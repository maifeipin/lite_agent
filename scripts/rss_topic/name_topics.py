#!/usr/bin/env python3
"""Step 4 (分类版): 用每 cluster 的标题样本调 DeepSeek 命名。运行于 vps1。
读 /tmp/topic_labels.json (doc_topic: {id:"cat::tid"}, clusters: {"cat::tid":{count,sample_titles}})。
clusters 里已预采样 12 条标题(classify_cluster.py 存的),无需再读 rss_all.jsonl。
输出 topic_names_cn (key->name) + doc_topic_name (id->name),写回 /tmp/topic_labels.json。
DEEPSEEK_API_KEY 从环境变量读。用法:
  scp topic_labels.json vps1:/tmp/
  ssh vps1 'export DEEPSEEK_API_KEY="$(grep ^DEEPSEEK_API_KEY= /home/liteagent/lite_agent/.env|cut -d= -f2-)"; python3 /tmp/name_topics_v2.py'
"""
import os, json, time, urllib.request

KEY = os.environ["DEEPSEEK_API_KEY"]
BASE = "https://api.deepseek.com/v1/chat/completions"
TL = "/tmp/topic_labels.json"
CACHE = "/home/liteagent/rss_topic_work/topic_names_cache.json"

data = json.load(open(TL, encoding="utf-8"))
doc_topic = data["doc_topic"]      # {id: "cat::tid"}
clusters = data["clusters"]        # {"cat::tid": {count, keywords, sample_titles}}

names = {}
if os.path.exists(CACHE):
    names = json.load(open(CACHE, encoding="utf-8"))
print("clusters={} cached_names={}".format(len(clusters), len(names)), flush=True)

PROMPT = ("以下是一组 RSS 文章的标题样本(可能含少量噪声):\n{titles}\n\n"
          "请归纳这组文章的共同主题,起一个 2-6 个字的中文主题名"
          "(如「大模型应用」「前端开发」「融资动态」「科技资讯」),"
          "只返回主题名本身,不要引号、解释、标点。")

def deepseek_name(titles):
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": PROMPT.format(titles="\n".join(titles))}],
        "max_tokens": 20, "temperature": 0.1,
    }).encode()
    r = urllib.request.Request(BASE, data=body, method="POST",
                               headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        d = json.loads(resp.read())
    return d["choices"][0]["message"]["content"].strip().strip("\"'""''。., ").replace("\n", "")

# 按文档数降序命名
order = sorted(clusters.keys(), key=lambda k: -clusters[k].get("count", 0))
for key in order:
    if key.endswith("::-1"):
        names[key] = "未分类"
        continue
    if key in names:
        continue
    titles = [t for t in clusters[key].get("sample_titles", []) if t]
    if not titles:
        names[key] = "未分类"
        continue
    for attempt in range(3):
        try:
            name = deepseek_name(titles[:12])
            if len(name) > 12:
                name = name[:12]
            names[key] = name or "未分类"
            print("{} ({} docs) -> {}  | e.g. {!r}".format(key, clusters[key]["count"], name, titles[0][:30]), flush=True)
            break
        except Exception as e:
            print("  {} err: {}".format(key, e), flush=True)
            time.sleep(2 ** attempt)
    else:
        names[key] = "未分类"
    json.dump(names, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    time.sleep(0.2)

# id -> name
doc_topic_name = {did: names.get(key, "未分类") for did, key in doc_topic.items()}

data["topic_names_cn"] = names          # "cat::tid" -> name
data["doc_topic_name"] = doc_topic_name  # id -> name
json.dump(data, open(TL, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("\nDONE named {} clusters, {} docs mapped".format(len(names), len(doc_topic_name)), flush=True)

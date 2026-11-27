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
from paths import cfg

TL = cfg("vps_tmp_dir", "/tmp") + "/topic_labels.json"
CACHE = cfg("vps_work_dir", "/home/liteagent/rss_topic_work") + "/topic_names_cache.json"

with open(TL, encoding="utf-8") as f:
    data = json.load(f)
mode = data.get("mode", "daily")
doc_topic = data["doc_topic"]      # {id: "cat::tid"}
clusters = data["clusters"]        # {"cat::tid": {count, keywords, sample_titles}}

names = {}
if mode == "weekly":
    print("mode=weekly: ignoring old cache, renaming {} clusters".format(len(clusters)), flush=True)
else:
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            names = json.load(f)
    print("mode={} clusters={} cached_names={}".format(mode, len(clusters), len(names)), flush=True)

PROMPT = ("以下是一组 RSS 文章的标题样本(可能含少量噪声):\n{titles}\n\n"
          "请归纳这组文章的共同主题,起一个 2-6 个字的中文主题名"
          "(如「大模型应用」「前端开发」「融资动态」「科技资讯」),"
          "只返回主题名本身,不要引号、解释、标点。")

def llm_name(titles):
    # Try Doubao (Ark) first
    key = os.environ.get("ARK_API_KEY") or os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")
    model = os.environ.get("LLM_MODEL", "doubao-seed-2.0-mini")
    
    if not key:
        # Fallback to DeepSeek
        key = os.environ.get("DEEPSEEK_API_KEY")
        base_url = "https://api.deepseek.com/v1/chat/completions"
        model = "deepseek-chat"
        
    if not key:
        raise ValueError("No LLM API Key found (ARK_API_KEY or DEEPSEEK_API_KEY)")
        
    if not base_url:
        base_url = "https://ark.cn-beijing.volces.com/api/v3"
        
    if not base_url.endswith("/chat/completions"):
        endpoint = base_url.rstrip("/") + "/chat/completions"
    else:
        endpoint = base_url
        
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(titles="\n".join(titles))}],
        "max_tokens": 20, "temperature": 0.1,
    }).encode()
    
    r = urllib.request.Request(endpoint, data=body, method="POST",
                               headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    return d["choices"][0]["message"]["content"].strip().strip("\"'""''。., ").replace("\n", "")

# 按文档数降序命名
order = sorted(clusters.keys(), key=lambda k: -clusters[k].get("count", 0))
target_n = 0
renamed = 0
failed = 0
for key in order:
    if key.endswith("::-1"):
        names[key] = "未分类"
        continue
    if mode != "weekly" and key in names:
        continue
    titles = [t for t in clusters[key].get("sample_titles", []) if t]
    if not titles:
        names[key] = "未分类"
        continue
    target_n += 1
    for attempt in range(3):
        try:
            name = llm_name(titles[:12])
            if len(name) > 12:
                name = name[:12]
            names[key] = name or "未分类"
            renamed += 1
            print("{} ({} docs) -> {}  | e.g. {!r}".format(key, clusters[key]["count"], names[key], titles[0][:30]), flush=True)
            break
        except Exception as e:
            print("  {} err (attempt {}/3): {}".format(key, attempt + 1, e), flush=True)
            time.sleep(2 ** attempt)
    else:
        failed += 1
        names[key] = "未分类"
        print("  {} fallback -> 未分类 (all retries failed)".format(key), flush=True)
    time.sleep(0.2)

# 熔断保护: 避免 LLM 欠费/故障时把旧缓存和全量文档刷成「未分类」
if target_n > 0 and (failed / target_n > 0.5 or renamed == 0):
    print("FATAL: naming circuit-breaker triggered (mode={}, target={}, renamed={}, failed={}); preserving old cache & topic_labels.".format(
        mode, target_n, renamed, failed), flush=True)
    raise SystemExit(1)

# 完成后原子写入缓存 (weekly 覆盖旧缓存; daily 仅在有新增命名时更新)
if mode == "weekly" or renamed > 0 or failed > 0:
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp_cache = CACHE + ".tmp"
    with open(tmp_cache, "w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False, indent=2)
    os.replace(tmp_cache, CACHE)

# id -> name
doc_topic_name = {did: names.get(key, "未分类") for did, key in doc_topic.items()}

data["topic_names_cn"] = names          # "cat::tid" -> name
data["doc_topic_name"] = doc_topic_name  # id -> name
with open(TL, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print("\nDONE mode={} named {} clusters (renamed={}, failed={}), {} docs mapped".format(
    mode, len(names), renamed, failed, len(doc_topic_name)), flush=True)

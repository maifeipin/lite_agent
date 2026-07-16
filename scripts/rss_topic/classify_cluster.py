#!/usr/bin/env python3
"""分类先于聚类:Layer 1(source->category) + 分类内 BERTopic。
运行于 Mac。读 ~/projects/rss_topic/rss_all.jsonl。

--mode weekly (默认): 全量 fit_transform 每类 BERTopic, 保存 7 个 per-category 模型到
                     topic_model/{cat}/, 输出 topic_labels.json (全量 doc_category+doc_topic+clusters)。
--mode daily:        rss_all.jsonl 只含新文章。加载已存 per-category 模型, 按新文 category 路由,
                     model.transform(embeddings) 预测 -> 归到已有 topic。只更新新文, 不重聚类。
                     模型不存在时该类新文 fallback -1(未分类)。

复用缓存 embeddings.npy(weekly, doc_ids 一致则跳过重嵌); daily 嵌入新文不写缓存。
"""
import os, json, argparse, random
from collections import defaultdict, Counter
import numpy as np

WORK = os.path.expanduser("~/projects/rss_topic")
DATA = WORK + "/rss_all.jsonl"
EMB = WORK + "/embeddings.npy"
IDS = WORK + "/doc_ids.json"
OUT = WORK + "/topic_labels.json"
MODEL_ROOT = WORK + "/topic_model"

ap = argparse.ArgumentParser()
ap.add_argument("--mode", choices=["daily", "weekly"], default="weekly")
ap.add_argument("--reembed", action="store_true", help="weekly 下强制重嵌(忽略缓存)")
args = ap.parse_args()
MODE = args.mode

# ---- Layer 1: source -> category ----
SOURCE_MAP = {
    "xiaohongshu.com": "社交短视频", "bilibili.com": "社交短视频", "douyin.com": "社交短视频",
    "douban.com": "社交短视频", "kuaishou.com": "社交短视频", "weibo.com": "社交短视频",
    "link.baai.ac.cn": "AI与学术", "alphaxiv.org": "AI与学术", "arxiv.org": "AI与学术",
    "baai.ac.cn": "AI与学术", "openreview.net": "AI与学术",
    "linux.do": "技术社区", "v2ex.com": "技术社区", "juejin.cn": "技术社区",
    "blog.csdn.net": "技术社区", "oschina.net": "技术社区", "infoq.com": "技术社区",
    "reddit.com": "技术社区", "eibo.com": "技术社区", "github.com": "技术社区",
    "segmentfault.com": "技术社区", "cnblogs.com": "技术社区", "jianshu.com": "技术社区",
    "ithome.com": "科技资讯", "36kr.com": "科技资讯", "huxiu.com": "科技资讯",
    "tmtpost.com": "科技资讯", "jiqizhixin.com": "科技资讯", "qbitai.com": "科技资讯",
    "toutiao.com": "科技资讯", "bbc.co.uk": "科技资讯", "bbc.com": "科技资讯",
    "zaobao.com.sg": "科技资讯", "ifanr.com": "科技资讯", "leiphone.com": "科技资讯",
    "pingwest.com": "科技资讯", "geekpark.net": "科技资讯", "donews.com": "科技资讯",
    "xueqiu.com": "财经商业", "oshipm.com": "财经商业", "eastmoney.com": "财经商业",
    "zhihu.com": "问答长文", "chongbuluo.com": "问答长文",
}

STOP = ['的','了','在','是','我','有','和','就','不','人','都','一','一个','上','也','很','到','说','要','去',
        '你','会','着','没有','看','好','自己','这','我们','他们','可以','这个','什么','一下','时候']


def load_docs():
    docs = []
    with open(DATA, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            title = (d.get("title") or "").strip()
            ex = (d.get("excerpt") or "").strip()
            docs.append({"id": d["id"], "title": title, "source": d.get("source", ""),
                         "text": (title + " " + ex).strip() or "无标题"})
    return docs


def make_vectorizer():
    import jieba
    from sklearn.feature_extraction.text import CountVectorizer
    jieba.setLogLevel("ERROR")
    return CountVectorizer(tokenizer=lambda t: jieba.lcut(t), max_features=10000,
                           stop_words=STOP, token_pattern=None)


def get_embeddings(docs, ids):
    """weekly: 复用缓存(doc_ids 一致则跳过); 否则全量嵌并存缓存。daily: 嵌新文, 不写缓存。"""
    if MODE == "weekly" and not args.reembed and os.path.exists(EMB) and os.path.exists(IDS):
        cached_ids = json.load(open(IDS))
        if cached_ids == ids:
            print("  using cached embeddings: {}".format(np.load(EMB).shape), flush=True)
            return np.load(EMB)
    # 需要嵌入
    from sentence_transformers import SentenceTransformer
    print("  embedding on MPS ...", flush=True)
    model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2", device="mps")
    texts = [d["text"] for d in docs]
    emb = model.encode(texts, batch_size=128, show_progress_bar=True, convert_to_numpy=True)
    if MODE == "weekly":
        np.save(EMB, emb)
        json.dump(ids, open(IDS, "w"))
        print("  saved cache: {} (weekly)".format(emb.shape), flush=True)
    else:
        print("  daily: embedded {} new docs (not caching)".format(emb.shape), flush=True)
    return emb


def main():
    print("##### mode={} #####".format(MODE), flush=True)
    docs = load_docs()
    ids = [d["id"] for d in docs]
    print("  {} docs".format(len(docs)), flush=True)
    emb = get_embeddings(docs, ids)

    # Layer 1 分类
    cat_idx = defaultdict(list)   # cat -> [(doc_i, emb_row)]
    unmapped = Counter()
    for i, d in enumerate(docs):
        cat = SOURCE_MAP.get(d["source"], "其他")
        if d["source"] not in SOURCE_MAP:
            unmapped[d["source"]] += 1
        cat_idx[cat].append(i)
    print("  categories: " + ", ".join("{}={}".format(c, len(v)) for c, v in
          sorted(cat_idx.items(), key=lambda x: -len(x[1]))), flush=True)

    doc_category = {}
    doc_topic = {}
    clusters = {}
    per_cat_stats = {}

    if MODE == "weekly":
        from bertopic import BERTopic
        rng = random.Random(42)
        for cat in sorted(cat_idx, key=lambda c: -len(cat_idx[c])):
            idxs = cat_idx[cat]
            n = len(idxs)
            texts = [docs[i]["text"] for i in idxs]
            em = emb[idxs]
            mts = max(30, n // 600)
            print("\n##### weekly {} (n={}, min_topic_size={}) #####".format(cat, n, mts), flush=True)
            tm = BERTopic(embedding_model=None, vectorizer_model=make_vectorizer(),
                          nr_topics="auto", min_topic_size=mts, n_gram_range=(1, 2),
                          calculate_probabilities=False, verbose=True)
            try:
                topics, _ = tm.fit_transform(texts, embeddings=em)
            except Exception as e:
                print("  FAILED {}: {} -> all -1".format(cat, e), flush=True)
                topics = [-1] * n
                info = type("I", (), {"Topic": [-1], "Count": [n]})()
                reps = {}
            else:
                info = tm.get_topic_info()
                reps = tm.topic_representations_
                # 保存 per-category 模型供 daily transform
                mdir = "{}/{}".format(MODEL_ROOT, cat)
                os.makedirs(mdir, exist_ok=True)
                tm.save(mdir, save_embedding_model=False)
                print("  saved model -> {}".format(mdir), flush=True)
            n_topics = len(info[info.Topic != -1])
            outlier = int(info[info.Topic == -1]["Count"].sum()) if (-1 in set(info.Topic)) else 0
            print("  -> {} topics, outlier {} ({:.1f}%)".format(n_topics, outlier, 100 * outlier / n), flush=True)
            per_cat_stats[cat] = {"n": n, "n_topics": n_topics, "outlier": outlier}

            titles_by_t = defaultdict(list)
            for k, t in enumerate(topics):
                did = docs[idxs[k]]["id"]
                doc_category[did] = cat
                doc_topic[did] = "{}::{}".format(cat, int(t))
                titles_by_t[int(t)].append(docs[idxs[k]]["title"])
            for t, cnt in zip(info.Topic, info.Count):
                key = "{}::{}".format(cat, int(t))
                kws = [w for w, _ in reps.get(int(t), [])][:10]
                st = list(titles_by_t.get(int(t), []))
                rng.shuffle(st)
                clusters[key] = {"count": int(cnt), "keywords": kws, "sample_titles": st[:12]}
            del tm, texts, em

    else:  # daily
        from bertopic import BERTopic
        loaded = {}  # cat -> BERTopic model
        for cat in sorted(cat_idx, key=lambda c: -len(cat_idx[c])):
            idxs = cat_idx[cat]
            n = len(idxs)
            texts = [docs[i]["text"] for i in idxs]
            em = emb[idxs]
            mdir = "{}/{}".format(MODEL_ROOT, cat)
            print("\n##### daily {} (n={} new) #####".format(cat, n), flush=True)
            if not os.path.exists(mdir):
                print("  no saved model for {} -> all -1".format(cat), flush=True)
                topics = [-1] * n
            else:
                if cat not in loaded:
                    loaded[cat] = BERTopic.load(mdir)
                try:
                    topics, _ = loaded[cat].transform(texts, embeddings=em)
                except Exception as e:
                    print("  transform FAILED {}: {} -> all -1".format(cat, e), flush=True)
                    topics = [-1] * n
            from collections import Counter as _C
            tdist = _C(int(t) for t in topics)
            print("  -> " + ", ".join("t{}={}".format(t, c) for t, c in tdist.most_common(6)), flush=True)
            per_cat_stats[cat] = {"n": n, "topic_dist": dict(tdist)}
            for k, t in enumerate(topics):
                did = docs[idxs[k]]["id"]
                doc_category[did] = cat
                doc_topic[did] = "{}::{}".format(cat, int(t))
            del em

    out = {
        "mode": MODE,
        "doc_category": doc_category,
        "doc_topic": doc_topic,
        "clusters": clusters,
        "n_docs": len(docs),
        "per_cat_stats": per_cat_stats,
        "source_map_used": SOURCE_MAP,
    }
    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=2)
    print("\n=== DONE {} saved {} ({} docs) ===".format(MODE, OUT, len(docs)), flush=True)
    for cat, s in per_cat_stats.items():
        print("  {}: {}".format(cat, s), flush=True)


if __name__ == "__main__":
    main()

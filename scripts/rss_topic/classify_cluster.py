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
import os, json, argparse, random, sys
from collections import defaultdict, Counter
import numpy as np
from paths import cfg

WORK = os.path.expanduser(cfg("mac_work_dir", "~/projects/rss_topic"))
DATA = WORK + "/rss_all.jsonl"
EMB = WORK + "/embeddings.npy"
IDS = WORK + "/doc_ids.json"
OUT = WORK + "/topic_labels.json"
MODEL_ROOT = WORK + "/topic_model"
MODEL_NAME = "BAAI/bge-base-zh-v1.5"   # 换模型时改这里, dim 不符会让缓存自动失效
MODEL_DIM = 768

ap = argparse.ArgumentParser()
ap.add_argument("--mode", choices=["daily", "weekly"], default="weekly")
ap.add_argument("--reembed", action="store_true", help="weekly 下强制重嵌(忽略缓存)")
args = ap.parse_args()
MODE = args.mode

# ---- Layer 1: source -> category ----
SOURCE_MAP = {
    # 社交短视频
    "xiaohongshu.com": "社交短视频", "bilibili.com": "社交短视频", "douyin.com": "社交短视频",
    "douban.com": "社交短视频", "kuaishou.com": "社交短视频", "weibo.com": "社交短视频",
    
    # AI与学术
    "link.baai.ac.cn": "AI与学术", "alphaxiv.org": "AI与学术", "arxiv.org": "AI与学术",
    "baai.ac.cn": "AI与学术", "openreview.net": "AI与学术", "qbitai.com": "AI与学术",
    "jiqizhixin.com": "AI与学术", "aidaily.win": "AI与学术",
    
    # 技术社区
    "linux.do": "技术社区", "v2ex.com": "技术社区", "juejin.cn": "技术社区",
    "blog.csdn.net": "技术社区", "oschina.net": "技术社区", "infoq.com": "技术社区",
    "reddit.com": "技术社区", "github.com": "技术社区", "hnrss.org": "技术社区",
    "segmentfault.com": "技术社区", "cnblogs.com": "技术社区", "jianshu.com": "技术社区",
    
    # 科技资讯
    "ithome.com": "科技资讯", "36kr.com": "科技资讯", "huxiu.com": "科技资讯",
    "tmtpost.com": "科技资讯", "toutiao.com": "科技资讯", "bbc.co.uk": "科技资讯",
    "bbc.com": "科技资讯", "zaobao.com.sg": "科技资讯", "ifanr.com": "科技资讯",
    "leiphone.com": "科技资讯", "pingwest.com": "科技资讯", "geekpark.net": "科技资讯",
    "donews.com": "科技资讯", "woshipm.com": "科技资讯", "uisdc.com": "科技资讯",
    "163.com": "科技资讯", "baidu.com": "科技资讯",
    
    # 财经商业
    "xueqiu.com": "财经商业", "eastmoney.com": "财经商业", "cls.cn": "财经商业",
    "nytimes.com": "财经商业",
    
    # 问答长文
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


def jieba_tokenizer(text):
    """模块级命名函数(非 lambda), 这样 BERTopic 模型才能 pickle save/load。"""
    import jieba
    return jieba.lcut(text)


def make_vectorizer():
    from sklearn.feature_extraction.text import CountVectorizer
    import jieba
    jieba.setLogLevel("ERROR")
    return CountVectorizer(tokenizer=jieba_tokenizer, max_features=10000,
                           stop_words=STOP, token_pattern=None)


def _load_cache():
    """load (emb, ids) 缓存; 维度不符(换模型)/损坏 -> (None, None)。"""
    if not (os.path.exists(EMB) and os.path.exists(IDS)):
        return None, None
    try:
        emb = np.load(EMB)
        ids = json.load(open(IDS))
        if emb.shape[0] != len(ids) or emb.shape[1] != MODEL_DIM:
            print("  cache dim/size mismatch {} (expected {}d), ignore".format(
                emb.shape, MODEL_DIM), flush=True)
            return None, None
        return emb, ids
    except Exception as e:
        print("  cache load failed ({}), ignore".format(e), flush=True)
        return None, None


def get_embeddings(docs, ids):
    """weekly: 增量缓存--load 旧缓存, 只嵌 doc_ids 里新增的, 按当前 ids 顺序拼齐, 存对齐缓存。
    daily: 嵌新文(本就只有新文), 不写缓存(避免与 weekly 抢写)。
    --reembed: weekly 强制全量重嵌(换模型后用)。"""
    if MODE == "daily":
        from sentence_transformers import SentenceTransformer
        print("  embedding {} new docs on MPS (daily, not cached) ...".format(len(docs)), flush=True)
        model = SentenceTransformer(MODEL_NAME, device="mps")
        return model.encode([d["text"] for d in docs], batch_size=128,
                            show_progress_bar=True, convert_to_numpy=True)

    # weekly: 增量缓存
    cached_emb, cached_ids = (None, None) if args.reembed else _load_cache()
    id2row = {cid: i for i, cid in enumerate(cached_ids)} if cached_ids else {}
    new_idx = [i for i, did in enumerate(ids) if did not in id2row]
    n_new = len(new_idx)

    if n_new:
        from sentence_transformers import SentenceTransformer
        print("  embedding {} new docs on MPS (cached={}, delta embed) ...".format(
            n_new, len(id2row)), flush=True)
        model = SentenceTransformer(MODEL_NAME, device="mps")
        new_emb = model.encode([docs[i]["text"] for i in new_idx], batch_size=128,
                               show_progress_bar=True, convert_to_numpy=True)
    else:
        print("  full cache hit: {} docs, 0 new (no embedding)".format(len(id2row)), flush=True)
        new_emb = None

    # 按当前 ids 顺序拼齐: 命中缓存取缓存行, 否则取新嵌行
    full = np.empty((len(ids), MODEL_DIM), dtype=np.float32)
    nc = 0
    for i, did in enumerate(ids):
        row = id2row.get(did)
        if row is not None:
            full[i] = cached_emb[row]
        else:
            full[i] = new_emb[nc]; nc += 1

    # 存对齐缓存(剪孤儿, 与当前 ids 完全对齐; 下次无新文则直接命中)
    np.save(EMB, full)
    json.dump(ids, open(IDS, "w"))
    print("  saved cache: {} (weekly, cached={} new={})".format(
        full.shape, len(id2row), n_new), flush=True)
    return full


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
        from umap import UMAP
        rng = random.Random(42)
        os.makedirs(MODEL_ROOT, exist_ok=True)
        for cat in sorted(cat_idx, key=lambda c: -len(cat_idx[c])):
            idxs = cat_idx[cat]
            n = len(idxs)
            texts = [docs[i]["text"] for i in idxs]
            em = emb[idxs]
            mts = max(30, n // 600)
            print("\n##### weekly {} (n={}, min_topic_size={}) #####".format(cat, n, mts), flush=True)
            tm = BERTopic(embedding_model=None, vectorizer_model=make_vectorizer(),
                          umap_model=UMAP(random_state=42, n_neighbors=15, n_components=5, metric="cosine"),
                          nr_topics="auto", min_topic_size=mts, n_gram_range=(1, 2),
                          calculate_probabilities=False, verbose=True)
            try:
                topics, _ = tm.fit_transform(texts, embeddings=em)
            except Exception as e:
                import pandas as pd
                print("  FAILED {}: {} -> all -1".format(cat, e), flush=True)
                topics = [-1] * n
                info = pd.DataFrame({"Topic": [-1], "Count": [n]})
                reps = {}
            else:
                info = tm.get_topic_info()
                reps = tm.topic_representations_
                # 保存 per-category 模型供 daily transform (pickle 文件, 不预建目录)
                mdir = "{}/{}".format(MODEL_ROOT, cat)
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

    # 后检(weekly): 真实 per-category 离群率, overall > 50% 中止(不存 topic_labels, 不 push, 保上轮)
    if MODE == "weekly":
        _tot = sum(s["n"] for s in per_cat_stats.values())
        _out_n = sum(s.get("outlier", 0) for s in per_cat_stats.values())
        _overall = _out_n / _tot if _tot else 0
        print("  postcheck: overall outlier={:.1%}".format(_overall), flush=True)
        if _overall > 0.5:
            print("FATAL: 整体离群率 {:.0%}, 保留上轮结果, 中止(不存 topic_labels)".format(_overall), flush=True)
            sys.exit(1)

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

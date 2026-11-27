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
import os, gc, json, argparse, random, sys
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
MAX_FIT_DOCS = int(os.environ.get("MAX_FIT_DOCS", "100000"))
TRANSFORM_BATCH_SIZE = int(os.environ.get("TRANSFORM_BATCH_SIZE", "50000"))
MIN_TOPIC_SIZE_FLOOR = 30
MIN_TOPIC_SIZE_CAP = 200

MODE = "weekly"
REEMBED = False

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

# ---- Layer 1.5: rssNodeId -> category override ----
NODE_MAP = {
    # V2EX 细粒度节点
    12: "技术社区",   # V2EX-程序员
    10: "科技资讯",   # V2EX-创意
    40: "科技资讯",   # V2EX-发现
    11: "科技资讯",   # V2EX-分享
    13: "问答长文",   # V2EX-问答
    42: "技术社区",   # V2EX-技术
    43: "社交短视频", # V2EX-好玩
    
    # LinuxDo 细粒度节点
    8: "技术社区",    # LinuxDo-最新话题
    9: "问答长文",    # LinuxDo-快问快答
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
                         "rssNodeId": d.get("rssNodeId"),
                         "date": d.get("date"),
                         "text": (title + " " + ex).strip() or "无标题"})
    return docs


def _doc_sort_key(doc, pos):
    """按时间新旧排序键: 优先 date 字段, 次选 24 位 ObjectId 前 8 位时间戳, 兜底行序。"""
    raw_date = doc.get("date")
    if raw_date is not None:
        try:
            dt = int(float(raw_date))
            if dt > 0:
                return (2, dt, pos)
        except (ValueError, TypeError):
            pass
    did = str(doc.get("id") or "")
    if len(did) == 24:
        try:
            return (1, int(did[:8], 16), pos)
        except ValueError:
            pass
    return (0, pos, pos)


def _select_fit_indices(idxs, docs, max_fit):
    """大类采样: 超过 max_fit 时优先选取最新的 max_fit 条做 fit, 其余做分块 transform。
    返回 (fit_pos, rest_pos), 均为 idxs 内升序下标, 保持 memmap 顺序读。"""
    n = len(idxs)
    if max_fit <= 0 or n <= max_fit:
        return list(range(n)), []
    ranked = sorted(range(n), key=lambda p: _doc_sort_key(docs[idxs[p]], p), reverse=True)
    fit_pos = sorted(ranked[:max_fit])
    rest_pos = sorted(ranked[max_fit:])
    return fit_pos, rest_pos


def _calc_min_topic_size(n_fit):
    """按训练样本量计算 min_topic_size, 加下限 30 与上限 200 保护细粒度热点。"""
    return min(MIN_TOPIC_SIZE_CAP, max(MIN_TOPIC_SIZE_FLOOR, n_fit // 600))


def _release_torch_memory(model=None):
    """显式释放 SentenceTransformer 与 MPS 统一内存缓存。"""
    if model is not None:
        del model
    gc.collect()
    try:
        import torch
        if hasattr(torch, "mps") and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


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
    """load (emb, ids) 缓存(mmap 只读避免双份常驻); 维度不符(换模型)/损坏 -> (None, None)。"""
    if not (os.path.exists(EMB) and os.path.exists(IDS)):
        return None, None
    try:
        emb = np.load(EMB, mmap_mode="r")
        with open(IDS, encoding="utf-8") as f:
            ids = json.load(f)
        if emb.ndim != 2 or emb.shape[0] != len(ids) or emb.shape[1] != MODEL_DIM:
            print("  cache dim/size mismatch {} (expected {}d), ignore".format(
                emb.shape, MODEL_DIM), flush=True)
            return None, None
        return emb, ids
    except Exception as e:
        print("  cache load failed ({}), ignore".format(e), flush=True)
        return None, None


def get_embeddings(docs, ids):
    """weekly: 增量缓存--mmap 只读旧缓存, 只嵌 doc_ids 里新增的, 按当前 ids 顺序写齐并重开 mmap。
    daily: 嵌新文(本就只有新文), 不写缓存(避免与 weekly 抢写)。
    --reembed: weekly 强制全量重嵌(换模型后用)。"""
    if MODE == "daily":
        from sentence_transformers import SentenceTransformer
        print("  embedding {} new docs on MPS (daily, not cached) ...".format(len(docs)), flush=True)
        model = SentenceTransformer(MODEL_NAME, device="mps")
        try:
            res = model.encode([d["text"] for d in docs], batch_size=128,
                               show_progress_bar=True, convert_to_numpy=True)
        finally:
            _release_torch_memory(model)
            model = None
        return res

    # weekly: 增量缓存
    cached_emb, cached_ids = (None, None) if REEMBED else _load_cache()
    id2row = {cid: i for i, cid in enumerate(cached_ids)} if cached_ids else {}
    new_idx = [i for i, did in enumerate(ids) if did not in id2row]
    n_new = len(new_idx)

    # 全命中且顺序一致: 直接复用只读 memmap, 零额外内存分配
    if n_new == 0 and cached_emb is not None and cached_ids == ids:
        print("  full cache hit (mmap): {} docs, 0 new (no embedding)".format(len(id2row)), flush=True)
        return cached_emb

    if n_new:
        from sentence_transformers import SentenceTransformer
        print("  embedding {} new docs on MPS (cached={}, delta embed) ...".format(
            n_new, len(id2row)), flush=True)
        model = SentenceTransformer(MODEL_NAME, device="mps")
        try:
            new_emb = model.encode([docs[i]["text"] for i in new_idx], batch_size=128,
                                   show_progress_bar=True, convert_to_numpy=True)
        finally:
            _release_torch_memory(model)
            model = None
    else:
        print("  full cache hit: {} docs, 0 new (reordering cache)".format(len(id2row)), flush=True)
        new_emb = None

    # 存对齐缓存(剪孤儿, 与当前 ids 完全对齐; 下次无新文则直接命中)
    # 通过 realpath 确保写入 NAS 真实同级目录的临时文件，再原子 rename，防止网络抖动损坏已有缓存
    real_emb = os.path.realpath(EMB)
    tmp_emb = real_emb[:-4] + ".tmp.npy" if real_emb.endswith(".npy") else real_emb + ".tmp"
    shape = (len(ids), MODEL_DIM)
    try:
        out_mm = np.lib.format.open_memmap(tmp_emb, mode="w+", dtype=np.float32, shape=shape)
        nc = 0
        for i, did in enumerate(ids):
            row = id2row.get(did)
            if row is not None:
                out_mm[i] = cached_emb[row]
            else:
                out_mm[i] = new_emb[nc]; nc += 1
        out_mm.flush()
        del out_mm
    except OSError as e:
        print("  warn: open_memmap write fallback ({}), using np.save".format(e), flush=True)
        full = np.empty(shape, dtype=np.float32)
        nc = 0
        for i, did in enumerate(ids):
            row = id2row.get(did)
            if row is not None:
                full[i] = cached_emb[row]
            else:
                full[i] = new_emb[nc]; nc += 1
        np.save(tmp_emb, full)
        del full

    del cached_emb, new_emb
    gc.collect()
    os.replace(tmp_emb, real_emb)
    with open(IDS, "w", encoding="utf-8") as f:
        json.dump(ids, f)
    print("  saved cache: {} (weekly, cached={} new={})".format(
        shape, len(id2row), n_new), flush=True)
    return np.load(EMB, mmap_mode="r")


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
        node_id = d.get("rssNodeId")
        try:
            node_id = int(node_id)
        except Exception:
            node_id = None
        cat = NODE_MAP.get(node_id) if node_id else None
        if not cat:
            cat = SOURCE_MAP.get(d["source"], "其他")
        if not cat or (node_id not in NODE_MAP and d["source"] not in SOURCE_MAP):
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
            fit_pos, rest_pos = _select_fit_indices(idxs, docs, MAX_FIT_DOCS)
            n_fit = len(fit_pos)
            mts = _calc_min_topic_size(n_fit)
            sampled_tag = ", sampled_fit={}/{}".format(n_fit, n) if rest_pos else ""
            print("\n##### weekly {} (n={}, min_topic_size={}{}) #####".format(
                cat, n, mts, sampled_tag), flush=True)
            fit_texts = [docs[idxs[p]]["text"] for p in fit_pos]
            fit_rows = [idxs[p] for p in fit_pos]
            em_fit = np.array(emb[fit_rows], dtype=np.float32, copy=True)
            tm = BERTopic(embedding_model=None, vectorizer_model=make_vectorizer(),
                          umap_model=UMAP(random_state=42, n_neighbors=15, n_components=5,
                                          metric="cosine", low_memory=True),
                          nr_topics="auto", min_topic_size=mts, n_gram_range=(1, 2),
                          calculate_probabilities=False, verbose=True)
            topics = [-1] * n
            try:
                fit_topics, _ = tm.fit_transform(fit_texts, embeddings=em_fit)
            except Exception as e:
                print("  FAILED {}: {} -> all -1".format(cat, e), flush=True)
                reps = {}
            else:
                for p, t in zip(fit_pos, fit_topics):
                    topics[p] = int(t)
                del fit_texts, em_fit, fit_topics
                fit_texts = em_fit = None
                gc.collect()

                reps = tm.topic_representations_
                # 大类剩余文档分块 transform 归类, 控制内存峰值
                if rest_pos:
                    batch_sz = max(1, TRANSFORM_BATCH_SIZE)
                    print("  transforming remaining {} docs in chunks of {} ...".format(
                        len(rest_pos), batch_sz), flush=True)
                    for start in range(0, len(rest_pos), batch_sz):
                        chunk_pos = rest_pos[start:start + batch_sz]
                        chunk_texts = [docs[idxs[p]]["text"] for p in chunk_pos]
                        chunk_rows = [idxs[p] for p in chunk_pos]
                        chunk_emb = np.array(emb[chunk_rows], dtype=np.float32, copy=True)
                        try:
                            chunk_topics, _ = tm.transform(chunk_texts, embeddings=chunk_emb)
                            for p, t in zip(chunk_pos, chunk_topics):
                                topics[p] = int(t)
                        except Exception as te:
                            print("  warn: chunk transform FAILED {} [{}..{}]: {} -> -1".format(
                                cat, start, start + len(chunk_pos), te), flush=True)
                        finally:
                            del chunk_texts, chunk_emb
                            gc.collect()

                # 保存 per-category 模型供 daily transform (pickle 文件, 不预建目录)
                # 用 realpath 确保在 NAS 真实同级目录原子 rename，防止写入中断损坏已有可用模型
                # 交换兼容文件/目录两种保存形态(当前 BERTopic 产出单文件, 直接 replace 已够用,
                # 但换新版本后 save 可能改产目录, rename 到非空目录会 ENOTEMPTY)。
                # 中断语义: 进程级中断下, 成功上位前上一代始终以 real 或 .old 保留,
                # 上位成功后才删除上一代。NAS 掉电的持久性取决于 SMB flush 顺序, 不在此承诺。
                import shutil

                def _rm_required(path):
                    """必成清理: 失败则中止本轮, 不触碰现役。"""
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    elif os.path.exists(path):
                        os.remove(path)

                def _rm_best_effort(path):
                    """尽力清理: 仅在现役已安全时调用, 失败只告警。"""
                    try:
                        _rm_required(path)
                    except OSError as e:
                        print("  warn: 清理 {} 失败: {}".format(path, e), flush=True)

                real_root = os.path.realpath(MODEL_ROOT)
                real_mdir = os.path.join(real_root, cat)
                tmp_mdir = real_mdir + ".tmp"
                old_mdir = real_mdir + ".old"
                # 0. 恢复: 上轮在两次 rename 之间中断(real 缺失)且 .old 仍在, 先让上一代
                #    复位为 real; 此后清理/保存失败均中止且不触碰现役。两次 rename 之间
                #    仍有短暂缺失窗口, 若在该窗口中断, daily 降级持续到下轮第 0 步恢复
                if not os.path.exists(real_mdir) and os.path.exists(old_mdir):
                    os.rename(old_mdir, real_mdir)
                _rm_required(tmp_mdir)                       # 残留不清干净则中止, 目录形态防新旧混入
                tm.save(tmp_mdir, save_embedding_model=False)
                _rm_required(old_mdir)                       # 让位目标必须为空, 失败则中止, 现役不动
                if os.path.exists(real_mdir):
                    os.rename(real_mdir, old_mdir)           # 现役让位
                try:
                    os.rename(tmp_mdir, real_mdir)           # 新版上位
                except OSError as promote_err:
                    if os.path.exists(old_mdir) and not os.path.exists(real_mdir):
                        try:
                            os.rename(old_mdir, real_mdir)   # 回滚完成后 daily 无感; 回滚前中断则缺失至下轮恢复
                        except OSError:
                            pass                            # 回滚也失败则保留 .old, 下轮第 0 步再恢复
                    raise promote_err
                _rm_best_effort(old_mdir)                    # 新版已安全, 尽力清理, 失败仅告警
                print("  saved model -> {}".format(real_mdir), flush=True)

            # 按真实全类预测结果(含采样 fit + 分块 transform)统计 topics、离群率与 clusters
            full_counts = Counter(int(t) for t in topics)
            n_topics = len([t for t in full_counts if t != -1])
            outlier = int(full_counts.get(-1, 0))
            print("  -> {} topics, outlier {} ({:.1f}%)".format(n_topics, outlier, 100 * outlier / n), flush=True)
            per_cat_stats[cat] = {"n": n, "n_fit": n_fit, "n_topics": n_topics, "outlier": outlier}

            titles_by_t = defaultdict(list)
            for k, t in enumerate(topics):
                did = docs[idxs[k]]["id"]
                doc_category[did] = cat
                doc_topic[did] = "{}::{}".format(cat, int(t))
                titles_by_t[int(t)].append(docs[idxs[k]]["title"])
            for t, cnt in full_counts.most_common():
                key = "{}::{}".format(cat, int(t))
                kws = [w for w, _ in reps.get(int(t), [])][:10]
                st = list(titles_by_t.get(int(t), []))
                rng.shuffle(st)
                clusters[key] = {"count": int(cnt), "keywords": kws, "sample_titles": st[:12]}
            del tm, fit_texts, em_fit
            gc.collect()

    else:  # daily
        from bertopic import BERTopic
        loaded = {}  # cat -> BERTopic model
        batch_sz = max(1, TRANSFORM_BATCH_SIZE)
        for cat in sorted(cat_idx, key=lambda c: -len(cat_idx[c])):
            idxs = cat_idx[cat]
            n = len(idxs)
            mdir = "{}/{}".format(MODEL_ROOT, cat)
            print("\n##### daily {} (n={} new) #####".format(cat, n), flush=True)
            if not os.path.exists(mdir):
                print("  no saved model for {} -> all -1".format(cat), flush=True)
                topics = [-1] * n
            else:
                if cat not in loaded:
                    try:
                        loaded[cat] = BERTopic.load(mdir)
                    except Exception as e:
                        print("  model load FAILED {}: {} -> all -1".format(cat, e), flush=True)
                        topics = [-1] * n
                        per_cat_stats[cat] = {"n": n, "topic_dist": {-1: n}}
                        for k, t in enumerate(topics):
                            did = docs[idxs[k]]["id"]
                            doc_category[did] = cat
                            doc_topic[did] = "{}::{}".format(cat, int(t))
                        continue
                topics = [-1] * n
                for start in range(0, n, batch_sz):
                    chunk_idxs = idxs[start:start + batch_sz]
                    chunk_texts = [docs[i]["text"] for i in chunk_idxs]
                    chunk_emb = np.array(emb[chunk_idxs], dtype=np.float32, copy=True)
                    try:
                        chunk_topics, _ = loaded[cat].transform(chunk_texts, embeddings=chunk_emb)
                        for offset_k, t in enumerate(chunk_topics):
                            topics[start + offset_k] = int(t)
                    except Exception as e:
                        print("  transform FAILED {}: {} -> -1".format(cat, e), flush=True)
                    finally:
                        del chunk_texts, chunk_emb
                        gc.collect()
            from collections import Counter as _C
            tdist = _C(int(t) for t in topics)
            print("  -> " + ", ".join("t{}={}".format(t, c) for t, c in tdist.most_common(6)), flush=True)
            per_cat_stats[cat] = {"n": n, "topic_dist": dict(tdist)}
            for k, t in enumerate(topics):
                did = docs[idxs[k]]["id"]
                doc_category[did] = cat
                doc_topic[did] = "{}::{}".format(cat, int(t))

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
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n=== DONE {} saved {} ({} docs) ===".format(MODE, OUT, len(docs)), flush=True)
    for cat, s in per_cat_stats.items():
        print("  {}: {}".format(cat, s), flush=True)


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "weekly"], default="weekly")
    ap.add_argument("--reembed", action="store_true", help="weekly 下强制重嵌(忽略缓存)")
    return ap.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    MODE = args.mode
    REEMBED = args.reembed
    main()

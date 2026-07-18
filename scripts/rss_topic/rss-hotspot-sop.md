# RSS 热点发现 SOP - 可复用操作手册 (as-built)

> 状态: 脚本已固化进仓库 `scripts/rss_topic/`(committed)。本 doc 是和代码对齐的 runbook。
> **关键修正**: 原 doc 的 Stage 1(mongoexport)/Stage 3(buggy 聚类)/Stage 4(关键词命名) 是早期设想, 实测有 bug/回退, 已废弃。以 `scripts/rss_topic/` 实测脚本为准。
> 每日/每周双模式: daily 调 `BERTopic.transform()`, weekly 调 `fit_transform()` + 存模型。其余 pipeline 复用。

---

## 架构

```
┌─ vps1 (轻依赖, stdlib) ─────────────────────────────┐
│  Stage 2: Meili 导出 id + Mongo 取 excerpt           │  step2a_export.py / step2b_enrich / step2c_backfill
│  Stage 4: DeepSeek 按标题样本命名                     │  name_topics.py
│  Stage 5: 写入 Meili + 热点检测 + 话题 diff            │  push_topics_v2.py / hotspot.py / topic_diff.py
│  (去重, 可选)                                        │  dedup_meili.py
└────────────────────┬───────────────────────────────┘
                     │ scp
┌─ Mac (ML 依赖, MPS) ▼──────────────────────────────┐
│  Stage 3: 分类内 BERTopic (--mode daily/weekly)       │  classify_cluster.py
│  环境: <mac_work_dir>/bertopic_env (pyenv 3.11)       │
└─────────────────────────────────────────────────────┘
所有 shell 命令统一走 run_rss2meili.sh, 在 Mac 上执行。
```

---

## 路径配置 (paths.py / paths.json)

为避免脚本和文档中的路径硬编码，本项目采用 `paths.py` 加载 `paths.json` 中配置的路径，并支持环境变量（前缀 `RSS_`）覆盖。

主要的共享路径参数如下：
* **`mac_work_dir`**（文档中用 `<mac_work_dir>` 表示，默认 `~/projects/rss_topic`）: Mac 本地保存模型、数据和虚拟环境的目录。
* **`vps_work_dir`**（文档中用 `<vps_work_dir>` 表示，默认 `/home/liteagent/rss_topic_work`）: VPS 上同步脚本及保存临时运行状态的目录。
* **`vps_tmp_dir`**（文档中用 `<vps_tmp_dir>` 表示，默认 `/tmp`）: VPS 上的临时文件夹。
* **`vps_env_file`**（文档中用 `<vps_env_file>` 表示，默认 `/home/liteagent/lite_agent/.env`）: VPS 上的环境变量/密钥配置文件。
* **`<repo_root>`**: Mac 本地 `lite_agent` 仓库的根目录。

---

## 两种运行模式

| 模式 | 频率 | 做什么 | 耗时 |
|---|---|---|---|
| **daily** | 每天 06:00 | 近 24h 新文 -> 加载已存模型 `transform()` 归到已有 topic -> 增量 push -> 热点检测 | ~5 min |
| **weekly** | 每周一 03:00 | 全量重聚类 `fit_transform()` + 存新模型 -> 命名 -> push -> 热点 + 话题 diff(新/消亡/涨跌) | ~35 min |

> 核心区别只有一行: daily 调 `.transform()`, weekly 调 `.fit_transform()`。daily 前提: weekly 至少跑过一次(有 `topic_model/{cat}/` 可 load)。

---

## 一键运行 (Mac)

```bash
bash scripts/rss_topic/run_rss2meili.sh daily           # 每日
bash scripts/rss_topic/run_rss2meili.sh weekly          # 每周
bash scripts/rss_topic/run_rss2meili.sh weekly --dedup  # 每周 + 去重
```
脚本自动把 vps1 侧脚本 scp 到 `vps1:<vps_work_dir>/`, 凭证从 vps1 `<vps_env_file>` 注入(不落盘)。

---

## 环境准备 (一次性)

### Mac
```bash
pyenv install 3.11.2
cd <mac_work_dir> && python3.11 -m venv bertopic_env && source bertopic_env/bin/activate
pip install bertopic==0.17 umap-learn hdbscan sentence-transformers jieba tqdm numpy
python -c "import torch; print(torch.backends.mps.is_available())"   # 必须 True
```

### vps1
```bash
ssh vps1 "echo ok"                    # SSH 免密
ssh vps1 "grep MEILI_MASTER_KEY <vps_env_file> | head -1"
ssh vps1 "grep RSSDB_URI <vps_env_file> | head -1"
```

### 目录结构
```
scripts/rss_topic/                    (仓库, committed - 脚本源头)
├── run_rss2meili.sh                  ← 主调度 (Mac)
├── step2_export_qdrant.py            ← 推荐: Qdrant 导出正文与向量 (--days N, vps1/local)
├── step2a_export.py                  ← [历史/备用] Meili 导出 (--days N, vps1)
├── step2b_mongo_enrich.py            ← [历史/备用] Mongo 取 excerpt -> rss_all.jsonl (vps1)
├── step2c_backfill.py                ← [历史/备用] Mongo 回补 content/published/date (vps1)
├── classify_cluster.py               ← BERTopic --mode daily/weekly (Mac)
├── name_topics.py                    ← DeepSeek 标题样本命名 (vps1)
├── push_topics_v2.py                 ← upsert category/topics/content (vps1)
├── hotspot.py                        ← 今日 vs 7日均值 热点检测 (vps1)
├── topic_diff.py                     ← weekly 话题新/消亡/涨跌 diff (vps1)
└── dedup_meili.py                    ← 按 link 去重 (vps1, 可选)

<mac_work_dir>/                        (Mac, 数据 + venv, gitignore 外)
├── bertopic_env/
├── rss_all.jsonl, embeddings.npy, doc_ids.json
├── topic_labels.json, topic_model/{cat}/   ← weekly 存的 per-category 模型

vps1:<vps_work_dir>/                   (vps1, 数据 + 部署的脚本)
├── meili_docs.jsonl, rss_all.jsonl, meili_backfill.jsonl
├── topic_names_cache.json             ← 命名缓存(daily 命名靠它, 持久)
├── topic_labels_prev.json             ← weekly diff 跨周基线(持久)
└── history/{YYYYMMDD}_topic_counts.json  ← 热点历史
```

---

## 各 Stage 详解

### Stage 2: Qdrant 导出文本与向量 (vps1 / local)
**优化**: 直接从 Qdrant Payload 提取清洗后的文章摘要 (`Excerpt`) 与预计算 768 维向量，完全跳过与 MongoDB 的低效查询。
```bash
# 推荐新链路 (单脚本一步完成导出):
python3 step2_export_qdrant.py --days 1 --with-vectors

# 兼容历史链路 (Meili + Mongo):
# python3 step2a_export.py --days 1 && python3 step2b_mongo_enrich.py && python3 step2c_backfill.py
```

### Stage 3: 分类内 BERTopic (Mac, classify_cluster.py --mode)
**修正**: 原 doc 的 stage3_cluster.py 有 bug(只取1个category、O(n²)重读文件、daily是stub)。用 `classify_cluster.py`。
- Layer 1: source -> category(7 类: 社交短视频/技术社区/AI与学术/科技资讯/财经商业/问答长文/其他)。
- Layer 2: 每类内 BERTopic(`embedding_model=None` 用预计算嵌入, `min_topic_size=max(30,n//600)`)。
- **weekly**: 全量 `fit_transform` + 保存 7 个 per-category 模型到 `topic_model/{cat}/`。
- **daily**: 加载模型, 按新文 category 路由, `model.transform(texts, embeddings=em)` 预测(归到已有 topic, 不重聚类)。
- 复用 `embeddings.npy` 缓存(weekly, doc_ids 一致则跳过重嵌); daily 嵌新文不写缓存。
```bash
cd <mac_work_dir> && source bertopic_env/bin/activate
python <repo_root>/scripts/rss_topic/classify_cluster.py --mode weekly   # 或 daily
```
- 产出 `topic_labels.json`: `{mode, doc_category, doc_topic("cat::tid"), clusters{count,keywords,sample_titles}, per_cat_stats}`。

### Stage 4: DeepSeek 命名 (vps1, name_topics.py)
**修正**: 不用 c-TF-IDF 关键词命名(关键词是乱码连写串)。用 **每 cluster 预采样的 12 条标题样本**喂 DeepSeek。
```bash
scp <mac_work_dir>/topic_labels.json vps1:<vps_tmp_dir>/topic_labels.json
ssh vps1 'export DEEPSEEK_API_KEY="$(grep ^DEEPSEEK_API_KEY= <vps_env_file>|cut -d= -f2-)";
         python3 <vps_work_dir>/name_topics.py'
```
- `cat::tid` 内部键防跨类 id 撞。cache `topic_names_cache.json`(持久)增量保存, 可断点续跑; **daily 命名走 cache**(已有 topic 名都在 cache, 无需新调 API)。

### Stage 5: 推送 Meili + 热点检测 (vps1)
```bash
# push (增量/全量由 topic_labels 覆盖范围决定: daily 只新文, weekly 全量)
ssh vps1 'export MEILI_MASTER_KEY="$(grep ^MEILI_MASTER_KEY= <vps_env_file>|cut -d= -f2-)";
          cd <vps_work_dir> && python3 push_topics_v2.py && python3 hotspot.py'
```
- push: PATCH filterable(含 category+topics) + 批量 upsert `{id, category, topics:[name], content, published, date}`。顺带回补 content。
- **hotspot.py**: 今日各 topic 计数(Meili facet, filter date>=今日0点) vs 过去 7 日 `history/` 均值; >3x 且日常≥3 触发; 新话题(历史<2天)今日≥20 也算。命中 top5 推送 lite-agent。
- ⚠️ 大批量 push 后 Meili 重索引 ~5-10min, 等 `isIndexing:false` 再验 facet。

### Stage 5b: 话题变化 diff (vps1, weekly, topic_diff.py)
```bash
ssh vps1 'python3 <vps_work_dir>/topic_diff.py'
```
- 本轮 `topic_labels.json` vs 上轮 `topic_labels_prev.json`, 指纹=中文名+top5关键词。
- 输出: 延续/新增/消亡 + 涨跌 top5。把本轮存为下轮基线。

### 去重 (可选, vps1, dedup_meili.py)
RssAdapter 不按 link 去重 -> 重复。`dedup_meili.py` 按 link 保留最老 ObjectId, 删多余。默认 dry-run, `--apply` 真删。根因(RssAdapter)修好前定期跑。

---

## Crontab (Mac)

```bash
crontab -e
# 每日 06:00 热点检测
0 6 * * * /bin/bash <repo_root>/scripts/rss_topic/run_rss2meili.sh daily >> <mac_work_dir>/logs/daily.log 2>&1
# 每周一 03:00 全量重聚类 + diff
0 3 * * 1 /bin/bash <repo_root>/scripts/rss_topic/run_rss2meili.sh weekly >> <mac_work_dir>/logs/weekly.log 2>&1
```
(先 `mkdir -p <mac_work_dir>/logs`)

---

## 运维坑

1. **`lite-agent.service` 缓存 skills** - 改 `skills/*.py` 后必须 `sudo systemctl restart lite-agent.service`。
2. **`ops_meili_sync.py` filterable 硬编码** - rss 的 `filterableAttributes` 必须含 `category`+`topics`(仓库已改)。
3. **`faceting.maxValuesPerFacet` 默认 100** - topics 142 个, 设 500。
4. **去重根因未修** - RssAdapter 不按 link 去重, 重复会回来。dedup 是续命。
5. **daily 前提** - weekly 至少跑过一次(存了 `topic_model/{cat}/`), 否则 daily 无模型可 load, fallback 全 -1。

---

## Dashboard Facet 验证

- [ ] RSS tab: 🗂 分类 / 🏷 主题 / 📂 来源 三 facet
- [ ] 分类可下钻主题(filter category -> 看 topics)
- [ ] 多选跨组 AND, 搜索+facet 同时生效
- [ ] daily 热点≤5 条推送

---

## 二期 (可选, 未做)

低代码高回报, 每月 < ¥1:
- **模块 A 情感极性**: DeepSeek 批量分析 top3 热点话题正/中/负。
- **模块 B 每日简报**: LLM 写 300 字摘要替代零散推送。
- **模块 C 话题页**: Dashboard 加"今日简报"入口。
- **双模型**: 豆包 Lite(快) + DeepSeek V3(质量), 命名/简报分工。

二期再单独固化, 不在本批。

---

## 和 rss-topic-pipeline.md 的关系

`docs/rss-topic-pipeline.md` 是更早的 topic 管道 as-built(无热点/调度/diff)。本 doc 是其超集(加了 daily/weekly + 热点 + diff + 调度), 脚本统一在 `scripts/rss_topic/`。以本 doc 为准。

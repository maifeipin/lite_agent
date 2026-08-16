# Lite Agent 架构文档

> 本文档供 AI 编程助手与开发者在新会话中快速理解项目全貌。纯原生 Python 实现，零重型框架依赖，具备动态工具分流、执行审计账本与多 Agent 编排能力。

---

## 1. 项目定位与设计原则

个人 VPS 上的多通道私有化 AI 运维与智能助理引擎。通过飞书、钉钉、企业微信、Telegram、个人微信（iLink 官方协议）或内置 Web 控制台下发指令，Agent 自动完成意图识别、动态工具裁剪、模型分流，并调度本地服务器的各类运维、待办、账单与资讯技能。

**核心设计原则**：
1. **轻量自研，零重型框架**：不依赖 LangChain/LlamaIndex 等重型框架，核心控制流纯原生实现，透明易维护。
2. **动态工具分流 (RequestSelector)**：针对 80+ 工具全量注入带来的 Token 消耗与模型幻觉问题，采用三态领域分流与三层权限组装机制。
3. **全链路可审计 (ExecutionLedger)**：工具调用与执行步骤持久化落库，具备完备的可观测性与历史回溯能力。
4. **配置安全与模块解耦**：密钥在 `.env` 隔离，模型配置独立于 `conf.d/`，技能即单文件 Python 函数。

---

## 2. 完整目录结构

```
lite_agent/
├── main.py              # 入口: 初始化 Agent → 启动通道 → 注册 Cron → 启动 API
├── agent.py             # Agent 核心: 消息分流、AI 生成循环、会话与多轮上下文管理
├── session.py           # 会话管理: SQLite 存储、TTL 清理、上下文窗口管理
├── security.py          # 安全沙箱: 命令拦截、沙箱路径校验
├── config.example.json  # 基础配置模板
├── config.json           # 生产配置（gitignore）
├── requirements.txt
│
├── conf.d/              # 模型与任务路由独立配置目录
│   ├── llm.json.example           # 主对话模型配置
│   ├── task_routing.json.example  # 任务场景模型路由配置
│   └── committee.json.example     # 决策委员会多模型配置
│
├── core/                # 核心引擎与基础设施层
│   ├── agent_runtime.py     # 统一代理运行时与事件状态机
│   ├── request_selector.py  # 动态工具选择器 (三态分流/写意图守卫/Shadow 模式)
│   ├── execution_ledger.py  # 执行审计账本持久化
│   ├── task_orchestrator.py # DAG 任务编排器 (Planner -> Worker -> Aggregator)
│   ├── subtask_dag.py       # 子任务依赖拓扑图构建
│   ├── worker_agent.py      # 编排子任务执行 Agent
│   ├── model_router.py      # 模型路由分流与故障降级
│   ├── model_invoker.py     # 模型调用器 (OpenAI 协议封装/Token 估算)
│   ├── model_config.py      # 模型配置加载与动态解析
│   ├── skill_engine.py      # 技能引擎: @skill 装饰器/Tool Schema/反射执行
│   ├── cron_engine.py       # 定时引擎: CronJob + CronManager 单例
│   ├── alerts.py            # 全局跨通道告警分发
│   ├── config_loader.py     # 环境变量与 JSON 配置动态合并加载
│   ├── gemini_codec.py      # Gemini thought_signature 等元数据编解码
│   └── constants.py         # 系统全局路径常量
│
├── channels/            # 多通道接入层
│   ├── base.py              # BaseChannel 抽象基类
│   ├── feishu.py            # 飞书 WebSocket 长连接通道 (lark SDK)
│   ├── dingtalk.py          # 钉钉 Stream 长连接通道 (dingtalk-stream SDK)
│   ├── wecom.py             # 企业微信 HTTP 接收与推送
│   ├── telegram.py          # Telegram Long Polling 通道 (curl + socks5h)
│   ├── wechat.py            # 个人微信 iLink 通道 (Context-Token 出站管理)
│   ├── wechat_ilink.py      # 微信 iLink 官方协议客户端 (扫码/游标长轮询)
│   └── api.py               # REST API / SSE 流式接口 / Web 控制台后端
│
├── web_dashboard/       # 内置 Web 管理控制台 (前端单页应用)
│   ├── index.html           # 主界面 (支持流式聊天、会话切换、指令抽屉)
│   ├── login.html           # 登录鉴权页
│   ├── style.css            # 现代化响应式设计系统
│   ├── main.js              # 控制台前端核心逻辑
│   ├── components/          # 模块化前端组件 (chat.js, facets.js 等)
│   └── modules/             # 业务视图模块 (todos, socks5, monitor 等)
│
├── memory_engine/       # 长期记忆引擎
│   ├── engine.py            # MemoryEngine: ChromaDB 向量库 + LLM 蒸馏
│   ├── pipeline.py          # DistillPipeline (定时蒸馏与反思)
│   ├── store.py             # MemoryStore (SQLite + Chroma 双写)
│   └── feedback.py          # 对话反馈闭环
│
├── edge_node/           # 边缘节点哨兵载荷 (独立部署在远端 VPS)
│   ├── edge_sentinel.py     # 边缘定时巡检与拉取执行守护进程
│   ├── edge_crypto.py       # Ed25519 零信任加解密与验签
│   ├── edge_whitelist.py    # 边缘命令安全白名单过滤
│   └── whitelist.json       # 白名单规则定义
│
├── scripts/             # 运维与离线数据处理管线
│   └── rss_topic/           # BERTopic 离线热点聚类管线 (分类内聚类/LLM命名/Meili同步)
│
└── skills/              # 内置技能库 (32 个模块，80+ 项 Tools)
    ├── ops_rss.py           # RSS 资讯聚合与加权评分推送
    ├── ops_rss_node.py      # RSS 节点状态查询
    ├── ops_todo.py          # 待办任务全生命周期管理与 IM 简报
    ├── ops_billing.py       # 财务对账、账单报表与临期提醒
    ├── ops_mail_reader.py   # 邮件阅读与批量解析
    ├── ops_mail_search.py   # 邮件跨主题/发件人搜索
    ├── ops_mail_stats.py    # 邮箱健康与处理统计
    ├── ops_mail_list.py     # 邮件列表概览
    ├── ops_mail_reprocess.py# 邮件重新识别与入库
    ├── ops_sys.py           # 系统资源负载与进程状态
    ├── ops_logs.py          # 系统高级跨文件日志检索
    ├── ops_security.py      # SSH 爆破审查与异常登录审计
    ├── ops_sentinel.py      # 安全哨兵基线扫描与漂移检查
    ├── ops_fleet_audit.py   # 集群安全审查
    ├── ops_self_check.py    # `/check` 全方位 9 项健康体检
    ├── ops_backup.py        # 本地数据打包与百度网盘 (bdpan) 官方云端增量备份
    ├── ops_blog.py          # Halo 博客自动发布与文章备份
    ├── ops_web_clipper.py   # 网页搜索、Markdown 提取与 HedgeDoc 云端转存
    ├── ops_socks5.py        # Socks5 代理节点管理、健康检查与动态端口出站探测
    ├── ops_decision.py      # AI 决策委员会多模型协同审议与投票
    ├── ops_gaokao.py        # 高考位次、专业录取与推荐分析
    ├── ops_edge_cmd.py      # 边缘节点指令下发
    ├── ops_edge_health.py   # 边缘节点健康度监控
    ├── ops_workspace.py     # 工作区文件操作
    ├── ops_crontab.py       # Linux 系统 Crontab 管理
    ├── ops_llm.py           # 大模型 API 余额与 Token 消耗统计
    ├── ops_memory_distiller.py # 长期记忆蒸馏 CLI
    ├── ops_media.py         # 媒体库与 NAS 去重管理 (PostgreSQL)
    ├── ops_meili_sync.py    # Meilisearch 全文索引同步
    ├── ops_search.py        # 聚合搜索引擎
    └── ops_bypy.py          # 百度网盘旧版接口 (Legacy)
```

---

## 3. 核心架构与请求处理流

### 3.1 消息处理全景流

```
用户消息 (IM / Web / API)
  │
  ▼
通道接入层 (channels/*.py) ── 封装为 IncomingMessage
  │
  ▼
Agent.handle() 分流网关
  ├─ "::" 前缀 ──────► 技能直接调用 (绕过 LLM，秒级执行)
  ├─ "/" 前缀 ───────► 内置指令处理器 (check / cron / status / new 等)
  └─ 普通自然语言 ───► _stream_ai_loop (AI 推理与工具调用循环)
                          │
                          ├─► RequestSelector 动态工具分流
                          │     ├── 关键词与领域匹配 (Domain Map)
                          │     ├── 多轮上下文与写意图继承守卫
                          │     └── 三层组装 (Action > Default > Explicit)
                          │
                          ├─► ModelRouter & ModelInvoker (模型调度与调用)
                          │
                          ├─► SkillEngine (反射执行命中 Tool)
                          │     └─► ExecutionLedger (落库执行审计账本)
                          │
                          └─► 生成最终回复 (流式推送 / 卡片下发)
```

### 3.2 RequestSelector 动态工具选择器 (`core/request_selector.py`)

为了解决 80+ 个工具全量注入导致的中小模型干扰、Token 膨胀与误调风险，系统引入了 RequestSelector：

1. **三态选择结果**：
   * `[]`（空集）：明确为闲聊/纯文本问答，不注入任何工具；
   * `None`（全量）：意图不明确或多领域工具超限，回退全量注入；
   * `[...]`（子集）：明确命中特定领域，仅注入对应工具集。
2. **两级安全分层（按用户风险）**：
   * `default_tools`：查询类只读工具，默认直接注入；
   * `explicit_intent_tools`：涉及数据修改、推送或外部同步的工具，必须匹配用户显式动作词（或满足写意图继承守卫）才注入。
3. **多轮写意图继承守卫**：
   用户回复“*确认*”、“*好的*”时，必须同时满足「短确认词 + 上轮助手是确认问句 + 领域匹配」三条件，才允许继承上轮的写操作工具，防止写权限意外残留。
4. **影子运行 (Shadow Mode)**：
   支持 `LITE_AGENT_SELECTOR_SHADOW=1` 环境变量，在真实请求走全量工具的同时后台预演并收集统计指标，确保零风险过渡。

---

## 4. 关键子系统

### 4.1 AI 决策委员会 (`ops_decision.py` & `conf.d/committee.json`)
针对关键系统决策或复杂方案，系统支持拉起由多个不同家族模型（如 `glm-5.3`、`kimi-k3`、`deepseek-v3` 等）构成的决策委员会，并发获取各模型审议意见并自动汇总投票仲裁。

### 4.2 复杂任务编排器 (`core/task_orchestrator.py`)
对于跨领域、多步骤的长耗时任务：
1. **Planner**：根据任务目标自动拆解为 DAG（有向无环图）子任务依赖链；
2. **Worker**：多线程并发执行互不依赖的子任务节点；
3. **Aggregator**：汇总各节点输出并生成最终交付结果。

### 4.3 记忆引擎 (`memory_engine/`)
双层记忆架构：
* **短期记忆**：`session.py` 基于 SQLite 维护最近对话历史（带滑动窗口与 Token 截断）；
* **长期记忆**：`MemoryEngine` 基于 ChromaDB 向量库存储重要事实，夜间定时触发 `DistillPipeline` 对会话进行反思与提炼蒸馏。

### 4.4 BERTopic 离线热点发现与主题聚类管线 (`scripts/rss_topic/`)
为了对海量多源 RSS 资讯进行宏观主题提炼与热点态势追踪，系统内置了 5 阶段离线聚类管线：
1. **Stage 1 & 2 (数据抽取与特征计算)**：从 MongoDB 抽取近 24h / 近一周文章，导出 ID 并构建特征向量（Embeddings）；
2. **Stage 3 (双层聚类机制)**：
   * **Layer 1**：基于来源规则进行领域大类粗分类；
   * **Layer 2**：类内执行 **BERTopic + UMAP + HDBSCAN + jieba 中文分词** 细粒度主题聚类；
   * **双模式调度**：
     * `daily`（每日 06:00）：增量调用 `BERTopic.transform()` 归入已有主题模型，~5 分钟内快速产出；
     * `weekly`（每周一 03:00）：全量 `BERTopic.fit_transform()` 重新训练分类模型，并计算话题演化 diff（新增/消亡/热度涨跌）。
3. **Stage 4 (LLM 智能语义命名)**：调用 DeepSeek 大模型根据每个 Topic 的代表性标题样本提炼主题名称与标签；
4. **Stage 5 (Meilisearch 索引回填与热点检索)**：将主题标签与热点得分回填写入 Meilisearch，供 `/rss_topic` 指令（`ops_search.py`）与 Web 控制台秒级检索。

### 4.5 边缘节点哨兵体系 (Edge Sentinel & `edge_node/`)
针对多台远端 VPS（如 `vps2`, `vps3`, `bwg`, `oracle1`, `vps5` 等）的统一安全管理与只读运维，系统构建了**零信任 Pull 模型边缘哨兵架构**：
1. **零信任双层签名体系 (`edge_crypto.py`)**：
   * **热私钥 (Hot Key)**：中枢在下发只读白名单命令时自动签名（携带 `task_id`、`ts` 时间戳与不可变 `nonce` 防重放）；
   * **根私钥 (Root Key)**：离线保管，仅在下发高危命令或管道组合命令时由管理员手动签名授权；
   * 边缘节点部署对应公钥，任何未签名或验签失败的任务直接拒绝执行。
2. **边缘拉取与状态机机制 (`core/edge_db.py`)**：
   * 边缘节点不开放任何入站端口，运行 `edge_sentinel.py` 定期（每 5 分钟）向中枢 `GET /api/pull_task` 拉取待执行任务；
   * 中枢通过 SQLite 事务状态机（`pending` → `dispatched` → `done/failed`）维护任务生命周期，杜绝多节点并发抢单；
   * 边缘执行完毕后通过 `POST /api/report` 回传结果（带 exit_code、stdout、stderr 与主机负载指标）。
3. **Agent 运维调度技能**：
   * `ops_edge_cmd.py` (`edge_cmd(node, cmd)`)：Agent 向指定节点下发只读命令，支持 30s 同步等待（超时自动转为异步）；
   * `ops_edge_health.py` (`edge_health()`)：全局巡检所有边缘节点心跳健康度与时延。

---

## 5. Web 控制台与 API 通道 (`web_dashboard/` & `channels/api.py`)

系统内置了单页 Web 管理控制台 (SPA) 与兼容 OpenAI 规范的 REST/SSE API 服务，由 `channels/api.py` 内置 `ThreadingHTTPServer` 统一托管：

### 5.1 Web Dashboard 功能模块 (`web_dashboard/`)
* **📧 邮件中心 (`modules/emails.js`)**：邮件列表多维检索、安全渲染 HTML 原文、账单与重要邮件快速筛选。
* **📰 RSS 新闻与 BERTopic 热点简报 (`modules/rss.js`)**：全源资讯流浏览、今日 BERTopic 热点每日简报看板（话题聚类分析、情感极性、热度演化标签）。
* **✅ 待办看板 (`modules/todos.js`)**：待办事项全生命周期管理（CRUD、优先级、延期/搁置、快捷标记完成、分类标签筛选）。
* **💬 会话管理 (`modules/sessions.js`)**：跨通道（飞书/企微/钉钉/TG/微信/API）历史会话列表查看、对话明细追溯与单会话一键清理。
* **🧦 Socks5 代理监控 (`modules/socks5.js`)**：节点状态看板、动态出站探测、出口 IP echo 验证与故障节点排查。
* **核心交互组件 (`components/`)**：
  * `chat.js`：流式 AI 对话抽屉，支持 Markdown + KaTeX 数学公式渲染、打字机效果与 `/` 快捷指令唤起；
  * `facets.js`：全局多维 Facet 检索与智能聚合过滤器；
  * `terminal.js`：内嵌终端仿真器，供系统运维与直接执行指令；
  * `modal.js`：模态弹窗系统（用于 HTML 邮件原文安全沙箱预览、节点配置详情等）。

### 5.2 鉴权体系
* **Admin Token (`auth_token`)**：拥有最高管理权限，可访问全部控制台模块与执行所有运维技能；
* **Guest Token (`guest_token`)**：仅开放只读面板与白名单对话技能，用于外网演示或访客安全接入；
* **Edge Token (`edge_token`)**：专用于边缘节点的心跳上报与任务拉取隔离权限。

### 5.3 核心 API 路由清单
| 路由 | 方法 | 鉴权要求 | 说明 |
|---|---|---|---|
| `/v1/chat/completions` | POST | Bearer Token | OpenAI 标准兼容对话接口（支持 SSE 真流式事件与工具调用） |
| `/v1/models` | GET | Bearer Token | 获取可用模型清单 |
| `/api/v1/auth` | POST | 公开 | 控制台登录与 Token 校验 |
| `/api/v1/sessions` | GET/DELETE | Bearer Token | 会话列表查询与单会话清理 |
| `/api/v1/sessions/messages` | GET | Bearer Token | 会话历史消息详细记录 |
| `/api/v1/todos` | GET/POST/PATCH | Bearer Token | 待办任务查询、新增与状态更新 |
| `/api/v1/socks5` | GET/POST/DELETE | Bearer Token | Socks5 节点查询、维护与动态测试 |
| `/api/v1/socks5/active` | GET | Bearer Token | 当前活跃出口节点查询 |
| `/api/v1/socks5/health` | GET | Bearer Token | 节点连通性健康巡检 |
| `/api/v1/socks5/test` | GET | Bearer Token | 节点延迟与测速 |
| `/api/v1/email/html` | GET | Bearer Token | 邮件 HTML 原文安全渲染 |
| `/api/v1/rss/brief` | GET | Bearer Token | BERTopic 今日热点每日简报数据 |
| `/api/report` & `/api/pull_task` | POST/GET | Edge Token | 边缘 Edge 节点监控指标上报与任务下发 |
| `/api/v1/dashboard` | GET | 公开 | 获取指令注册表概览与静态资源 |

---

## 6. 定时任务与告警回退链

### 6.1 任务调度 (CronManager)
定时任务支持 Crontab 表达式与时间范围区间定义（如 `09:03-22:03` 每小时执行一次），支持执行系统 Shell 命令或反射调用 `skills/` 模块函数。

### 6.2 全局告警回退链 (`core/alerts.py`)
系统告警与定时推送按以下优先级依次尝试，直至送达：
```
飞书 (feishu) ──► 企业微信 (wecom) ──► 个人微信 (wechat) ──► 钉钉 (dingtalk) ──► Telegram (telegram)
```
*注：个人微信通道仅在对应管理员持有未过期的入站 `context_token` 时可接收推送。*

---

## 7. 部署与运维

### 7.1 systemd 服务配置

```ini
# /etc/systemd/system/lite-agent.service
[Unit]
Description=Lite Agent Service
After=network.target

[Service]
Type=simple
User=liteagent
WorkingDirectory=/home/liteagent/lite_agent
ExecStart=/usr/bin/python3 /home/liteagent/lite_agent/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 7.2 百度网盘官方 CLI (`bdpan`) 配置

云盘自动备份模块依赖百度官方 `bdpan` CLI：
```bash
# 1. 下载安装器 (Linux x86_64 示例)
curl -fsSL https://issuecdn.baidupcs.com/issue/netdisk/ai-bdpan/installer/3.8.4/bdpan-installer-linux-amd64 -o bdpan-installer
chmod +x bdpan-installer && ./bdpan-installer --yes
sudo ln -sf ~/.local/bin/bdpan /usr/local/bin/bdpan

# 2. 授权登录 (必须以运行服务的 liteagent 用户执行)
sudo -u liteagent -H bdpan login
# 验证状态: bdpan whoami
```

---

## 8. 当前功能就绪状态矩阵

| 模块 / 特性 | 状态 | 备注 |
|---|:---:|---|
| **飞书 / 钉钉 / 企业微信 / Telegram** | ✅ | 四大主流 IM 通道全部就绪 |
| **个人微信 iLink 通道** | ✅ | 官方扫码长轮询接入，支持 Context-Token 出站管理 |
| **Web 管理控制台 (Dashboard)** | ✅ | 单页 SPA，支持流式对话、会话切换与指令抽屉 |
| **OpenAI 兼容 API / SSE 流式** | ✅ | `/v1/chat/completions` 全兼容，支持 Guest 隔离 |
| **动态工具分流 (RequestSelector)** | 🔄 | Phase 1 Shadow 运行中，支持三态与写意图守卫 |
| **AI 决策委员会** | ✅ | 支持 GLM-5.3、Kimi-K3 等多模型投票决策 |
| **DAG 复杂任务编排** | ✅ | TaskOrchestrator 支持多子任务并发执行 |
| **执行审计账本 (ExecutionLedger)** | ✅ | 全量工具调用与执行步骤持久化落库 |
| **双层长短期记忆引擎** | ✅ | SQLite 对话历史 + ChromaDB 长期记忆蒸馏 |
| **全自动多模态视觉 (OCR)** | ✅ | 直传图片调外置 OCR 代理解析 |
| **百度网盘官方增量灾备** | ✅ | 基于 `bdpan` CLI 自动归档 Halo/DB/配置至云端 |
| **BERTopic 离线热点聚类管线** | ✅ | 支持 Daily 增量 transform 与 Weekly 全量重聚类及演化分析 |
| **Edge 边缘节点哨兵体系** | ✅ | 零信任 Ed25519 签名下发、拉取状态机与健康监控 |
| **Socks5 节点动态探测** | ✅ | 支持隔离端口出站探测与出口 IP 识别 |

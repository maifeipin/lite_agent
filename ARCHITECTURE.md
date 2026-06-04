# Lite Agent 架构文档

> 本文档供 AI 编程助手在新会话中快速理解项目全貌。约 5,200 行 Python，一个人维护。

---

## 1. 项目定位

个人 VPS 上的多通道 AI 运维助手。通过飞书/钉钉/企微/Telegram 任一 IM 下发指令，Agent 调度本地技能（运维、账单、RSS、记忆）执行并返回结果。

**设计原则**：零重型框架依赖、一个人可维护、配置驱动、技能即 Python 函数。

---

## 2. 目录结构

```
lite_agent/
├── main.py              # 入口: 加载配置 → 初始化 Agent → 启动通道 → 注册 Cron
├── agent.py             # Agent 核心: 消息路由、AI 循环、Tool Calling
├── session.py           # 会话管理: SQLite 存储、TTL、上下文窗口
├── skill_engine.py      # 技能引擎: @skill 装饰器 → Tool Schema → 函数调用
├── cron_engine.py       # 定时引擎: CronJob + CronManager 单例、HH:MM 匹配
├── security.py          # 安全: 命令沙箱、路径白名单
├── config.example.json  # 配置模板（含所有可配置项）
├── config.json           # 生产配置（gitignore）
├── requirements.txt
│
├── channels/            # IM 通道层
│   ├── base.py           # BaseChannel 抽象基类
│   ├── feishu.py         # 飞书 WebSocket (lark SDK)
│   ├── dingtalk.py       # 钉钉 Stream (dingtalk-stream SDK)
│   ├── wecom.py          # 企业微信 HTTP 回调 + pushmsg
│   ├── telegram.py       # Telegram Long Polling (subprocess+curl)
│   └── api.py            # 对外提供的 REST/SSE API (兼容 OpenAI)
│
├── memory_engine/       # 长期记忆引擎
│   ├── engine.py         # MemoryEngine: ChromaDB 向量库 + LLM 蒸馏
│   ├── pipeline.py       # DistillPipeline + DistillTrigger
│   ├── store.py          # MemoryStore: SQLite + Chroma 双写
│   ├── feedback.py       # 反馈闭环
│   └── lite_integration.py
│
└── skills/              # 技能库（每个文件自动注册）
    ├── ops_rss.py         # RSS 精选引擎 (300+ 行，最复杂)
    ├── ops_self_check.py  # 健康自检 (/check)
    ├── ops_billing.py     # 账单管理（调外部 mail-statement-parser）
    ├── ops_backup.py      # 数据备份
    ├── ops_sys.py         # 系统状态
    ├── ops_security.py    # 安全审查
    ├── ops_logs.py        # 日志检索
    ├── ops_crontab.py     # crontab 管理
    ├── ops_workspace.py   # 工作区文件操作
    ├── ops_llm.py         # API 余额查询
    ├── ops_memory_distiller.py  # 记忆蒸馏 CLI
    ├── ops_blog.py        # Halo 博客管理与发布
    ├── ops_bypy.py        # 百度网盘直连同步与备份
    └── ops_media.py       # 媒体库与NAS管理 (PostgreSQL)
```

---

## 3. 核心架构

### 3.1 消息流

```
用户发消息 (IM App)
  → 通道层 (channels/*.py) 接收，封 IncomingMessage
    → Agent.handle(msg) 路由
      ├── "::" 前缀 → _handle_double_colon (技能直接调用)
      ├── "/" 前缀 → _handle_builtin (内置指令)
      └── 其他     → _run_ai_loop (LLM Tool Calling)
    → AgentResponse
  → 通道层回复
```

### 3.2 IncomingMessage / AgentResponse

```python
# agent.py
class IncomingMessage:
    channel: str        # 'feishu'|'dingtalk'|'wecom'|'telegram'
    user_id: str        # 发信人 ID
    chat_id: str
    message_id: str
    text: str
    @property session_key → f"{channel}:{user_id}"

class AgentResponse:
    text: str
    title: str = ""
    color: str = "blue"  # 卡片颜色
```

### 3.3 BaseChannel 接口

```python
class BaseChannel(ABC):
    def start(self)           # 启动连接
    def stop(self)            # 停止
    def send_response(msg_id, AgentResponse) -> bool  # 回复
    def send_to(open_id, AgentResponse) -> bool       # 主动推送（可选）
    def broadcast(AgentResponse) -> bool              # 广播（可选）
    def send_progress(msg_id, text) -> bool           # 已收到回显（可选）
```

### 3.4 各通道实现对比

| 通道 | 接收方式 | 发送方式 | 代理需求 |
|------|---------|---------|---------|
| 飞书 | lark SDK WebSocket | SDK 卡片回复 | 直连 |
| 钉钉 | dingtalk-stream SDK | SDK 文本 | 直连 |
| 企微 | Flask w.py 回调 → POST :8899 | pushmsg :6969 HTTP API | 内网直连 |
| TG | subprocess+curl Long Polling | curl HTTP API | socks5h 代理 |
| API | 内置 ThreadingHTTPServer | 标准 JSON / SSE 兼容 OpenAI | 直连 |

**企微架构**（最复杂，独立于 Lite Agent 进程）：
```
企业微信 → w.py(:6008,Flask) → POST to Lite Agent(:8899) → WeComChannel
WeComChannel.send_to → pushmsg(:6969,Flask) → 企业微信 API
```

---

## 4. Skill Engine

### 4.1 注册技能

```python
@skill(name='ops_xxx', description='...', params={'arg1': {...}})
def ops_xxx(arg1: str) -> str:
    return "result"
```

`@skill` 装饰器自动：
1. 提取函数的 GPT Function Calling tool schema
2. 注册到 SkillEngine._skill_registry
3. 记录参数类型、默认值

### 4.2 AI 调用流程

LLM 返回 `tool_calls` → SkillEngine 查 registry → 调用 `fn(**args)` → 返回结果给 LLM 继续推理。最大 10 步循环。

### 4.3 内置指令（/）

| 指令 | 功能 |
|------|------|
| `/check` | 9 项健康自检 |
| `/cron` | 查看定时任务 |
| `/cron <n>` | 手动执行 |
| `/cron toggle <n>` | 启停 |
| `/balance` | API 余额 |
| `/status` | 会话状态 |
| `/history` | 对话历史 |
| `/new` | 重置会话 |
| `/help` | 帮助 |
| `/remember <type> <内容>` | 强制记忆 |

### 4.4 :: 直接技能调用（不走 AI）

```
::rss push     → 推送 RSS 精选
::rss ai       → 查看 AI 资讯 Top5
::rss v2ex     → 查看 V2EX Top5
::rss log      → 查看推送日志
::cron log     → 查看定时任务日志
```

---

## 5. 定时任务引擎

### 5.1 CronManager（单例）

```python
class CronJob:
    name, cron_expr, func, enabled, last_run_date

class CronManager:
    add_job(name, time, func)
    start()  # 后台线程，每分钟检查 HH:MM
    list_jobs()
    toggle_job(idx)
```

### 5.2 任务定义（config.json → cron_jobs）

两种类型：
- **command**: 执行 shell 命令，支持 `{root}` 占位符
- **skill**: 调用 `skills/` 模块函数

```json
{
  "cron_jobs": [
    {"name": "证书过期检查", "time": "09:00", "command": "bash /root/down/check_cert_expiry.sh"},
    {"name": "记忆蒸馏复盘", "time": "03:00", "command": "python3 {root}/skills/ops_memory_distiller.py --mode daily"},
    {"name": "RSS 精选推送", "time_range": {"start": 9, "end": 22, "minute": 3}, "skill": "ops_rss::rss_push"},
    {"name": "数据打包备份", "time": "03:00", "skill": "ops_backup::do_backup"}
  ]
}
```

`time_range`: 生成 `09:03, 10:03, ..., 22:03` 多条 cron job。

另外 `ops_backup.py` 和 `ops_memory_distiller.py` 也有模块级 `CronManager().add_job()` 注册。`main.py` 在启动时强制 `import skills.ops_backup` 确保这些注册生效。

---

## 6. 记忆引擎 (memory_engine/)

```
MemoryEngine (engine.py)
├── MemoryStore (store.py): 双写 SQLite + ChromaDB
├── DistillPipeline (pipeline.py): LLM 蒸馏 → 提取长期记忆
├── DistillTrigger (pipeline.py): 定时(每天 03:00) + 阈值(100条)
├── FeedbackLoop (feedback.py): 对话反思 → 修正记忆
└── lite_integration.py: LiteLLM 兼容适配
```

Agent 对话后自动存储，每天凌晨蒸馏复盘。依赖 `chromadb` + `sentence-transformers` (bge-small-zh-v1.5)。

---

## 7. 配置系统 (config.json)

### 7.1 完整 Schema

```json
{
  "bot_name": "VPS 助手",
  "project_root": "/root/lite_agent",
  "service_name": "lite-agent",

  "llm": {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-xxx",
    "model": "deepseek-v4-flash",
    "max_tokens": 2048,
    "temperature": 0.3
  },

  "session": {
    "ttl_minutes": 30,
    "max_history": 20,
    "max_steps_per_goal": 10,
    "daily_token_limit": 500000
  },

  "security": {
    "allowed_users": [],
    "sandbox_paths": ["/var/log", "/root/down", "/root/script"],
    "blocked_commands": ["rm -rf /", "mkfs", "dd if=", "> /dev/sd", "shutdown", "reboot", "passwd"]
  },

  "rssdb": {
    "uri": "mongodb://user:pass@localhost:27017",
    "database": "rsslite"
  },

  "v2ex": {
    "token": "YOUR_V2EX_TOKEN"
  },

  "cron_jobs": [{...}],

  "channels": {
    "feishu": {
      "enabled": true,
      "app_id": "cli_xxx",
      "app_secret": "xxx",
      "admin_open_id": "ou_xxx"
    },
    "dingtalk": {
      "enabled": false,
      "client_id": "dingxxx",
      "client_secret": "xxx"
    },
    "telegram": {
      "enabled": true,
      "bot_token": "xxx",
      "proxy": "socks5h://127.0.0.1:18988",
      "admin_chat_id": "123456789"
    },
    "wecom": {
      "enabled": true,
      "listen_port": 8899,
      "push_url": "http://127.0.0.1:6969/send_message",
      "push_token": "xxx"
    }
  }
}
```

### 7.2 config.json vs config.example.json
- `config.json`: 生产配置，含真实密钥，gitignore
- `config.example.json`: 模板文件，占位符，提交到仓库

---

## 8. 外部系统集成

### 8.1 VPS 上的独立服务

| 服务 | 位置 | 端口 | 说明 |
|------|------|------|------|
| MongoDB | localhost:27017 | RSS 数据存储 |
| w.py | /root/down/wx/weworkapi_python-master/w.py | :6008 | 企微回调接收 |
| pushmsg.py | /root/down/wx/weworkapi_python-master/pushmsg/ | :6969 | 企微消息发送 (text/markdown) |
| mail_client.py | /root/mail-statement-parser/ | CLI | 邮件账单解析 |
| qdrant | localhost | :6333 | 向量库（RssAdapter 用） |
| RssAdapter | /app/RssAdapter | systemd | RSS 爬虫（C#） |

### 8.2 外部监控

```bash
# crontab: 每 5 分钟检查 lite-agent 存活
*/5 * * * * python3 /root/alert/monitor_lite_agent.py >> /tmp/monitor_lite_agent.log 2>&1
```

独立于进程，systemctl is-active → 异常时走 pushmsg 发企微告警。

### 8.3 证书监控

bash 脚本 `/root/down/check_cert_expiry.sh` → 调 pushmsg API 发通知。作为 cron command 注册。

---

## 9. RSS 精选引擎 (ops_rss.py) - 重点模块

最复杂的技能（314 行），处理流程：

```
RssNode(MongoDB) → FeedItem(按日集合) → 评分 → 去重 → 缓存 → 推送
```

### 9.1 评分算法

```python
score = SITE_QUALITY[site]        # 站点基础分 (3~9)
score += sum(HOT_KEYWORDS in title)  # 关键词加分
score -= 10 if V2EX_LOW_TAGS in title  # 低质量标签降权
score += reply_bonus              # V2EX API 回复数加成
```

### 9.2 预计算缓存

- HH:50 预计算 → `rss_cache.json` (900s 有效)
- HH:03 读取缓存 → 秒级推送
- 缓存过期自动实时计算

### 9.3 V2EX API 集成

```python
# 通过 socks5h 代理调 V2EX API 获取回复数
V2EX_TOKEN = config['v2ex']['token']
# 回复数 >=20: +1, >=50: +3, >=100: +5
```

---

## 10. 关键设计决策

1. **不使用 Webhook 公网暴露**：飞书/钉钉用 WebSocket 主动出站连接，企微用内网 HTTP 桥接。VPS 无需公网 IP 和 Nginx 配置。

2. **curl + subprocess 做 HTTP**：TG 和 V2EX API 用 `subprocess.run(['curl', ...])` 而非 `requests`，因为 socks5h 代理在 Python 里不稳定（PySocks 与 urllib3 兼容问题）。

3. **配置驱动 Cron**：定时任务从 config.json 注册，加新任务只改配置不写代码。

4. **通道回退链**：`_send_card()` 按 `feishu → dingtalk → wecom → telegram` 顺序尝试，任一成功即停。

5. **单例 CronManager**：模块级 `CronManager()` 调用返回同一实例，确保 `main.py` 注册和 `ops_backup.py` 模块级注册共享同一调度器。

6. **Send-Only 通道**：企微和 TG 的接收路径不在 Lite Agent 进程内（w.py、TG long polling 在进程内），但发送统一通过 `send_to` 方法。

---

## 11. 会话管理 (session.py)

```python
class SessionManager:
    _connect() → sqlite3.Connection
    get_or_create(session_key) → Session
    reset_session(key)
    cleanup_expired()       # 后台线程每 5 分钟清理
    add_message(key, role, content)

class Session:
    messages: list[dict]    # [{"role":"user","content":"..."}, ...]
    token_count: int
    created_at, last_active: float
```

`data/sessions.db` 单文件 SQLite，TTL 30 分钟，最多 20 条历史。

---

## 12. AI 循环 (agent.py)

```python
Agent._run_ai_loop(msg) → AgentResponse:
    1. 构建 messages: system_prompt + 记忆 + 历史 + user_msg
    2. 调 LLM (OpenAI SDK)
    3. 如返回 tool_calls → SkillEngine 执行 → 结果回填 → 回到步骤 2
    4. 如返回 content → 存储记忆 → 返回 AgentResponse
    5. 最多 max_steps_per_goal (10) 步，防死循环
```

System prompt 从 `agent.py:AGENT_PROMPT` 常量生成，含 bot_name 和技能列表。

---

## 13. 部署

### 13.1 systemd service

```
# /etc/systemd/system/lite-agent.service
ExecStart=/usr/bin/python3 /root/lite_agent/main.py
Restart=always
RestartSec=5
```

### 13.2 依赖

```
openai>=1.0
lark-oapi
dingtalk-stream
chromadb
sentence-transformers
pymongo
PySocks
psutil
```

### 13.3 常用运维命令

```bash
systemctl restart lite-agent
journalctl -u lite-agent -f
scp file.py vps1:/root/lite_agent/   # 部署单文件
```

---

## 14. 当前状态 (v2.2)

| 功能 | 状态 |
|------|:---:|
| 飞书/钉钉/企微/TG 四条通道 | ✅ |
| 接收指令 + 回复 | ✅ |
| 定时推送 fallback 链 | ✅ |
| RSS 多源聚合评分 | ✅ |
| V2EX API 回复数加成 | ✅ |
| 低质量标签降权 | ✅ |
| 9 项健康自检 `/check` | ✅ |
| 长期记忆 + 蒸馏 | ✅ |
| 外部独立监控 | ✅ |
| 数据自动备份 | ✅ |
| 账单管理 | ✅ |
| 配置驱动 Cron | ✅ |
| 多模型路由 | ✅ (ModelRouter 支持跨模型调度与降级) |
| Web 管理面板 | ❌ |
| Web 控制台通道 / API | ✅ (完美兼容 OpenAI 接口与 Guest 模式) |
| OCR / TTS / STT | ❌ |
| 多 Agent 长任务 | ✅ (TaskOrchestrator 编排子任务) |
| 文件识别 | ❌ |
| 支付集成 | ❌ (计划中) |

---

## 15. Roadmap / 预留扩展点

1. **Web 管理面板**：Flask/FastAPI 独立进程，读 sessions.db，展示状态
2. **OCR/TTS/STT**：新增 `skills/ops_ocr.py` 等，调外部 API
3. **支付集成**：支付宝 SDK，`skills/ops_pay.py`

---

## 16. 新通道开发模板

1. 继承 `BaseChannel`，实现 `start/stop/send_response`
2. 可选 `send_to(open_id, response)` 用于定时推送
3. 在 `main.py` 加加载逻辑
4. 在 `config.example.json` 加配置段
5. 在 `_send_card()` 回退链加通道名

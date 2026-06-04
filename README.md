# Lite Agent

🚀 **Lite Agent** 是一个轻量级、零外部依赖（仅依赖官方 SDK）、支持深度思考大模型的私有化 AI 智能助手引擎。通过 WebSocket / HTTP 回调接入**飞书、钉钉、企业微信**三大国内 IM，并通过自然语言全自动调度本地服务器的运维、账单、RSS 精选等技能。

![效果演示](assets/screenshot.png)
## 🌟 核心特性

- **极致轻量**: 核心框架使用 Python 内置库（`urllib`, `sqlite3`, `threading`, `http.server` 等），无需庞大三方框架。
- **动态技能引擎 (Skill Engine)**: 编写普通 Python 函数加 `@skill` 装饰器，一秒将本地脚本转化为 AI 可用工具。
- **完美适配 DeepSeek**: 严格遵循 DeepSeek Tool Calling 规范，支持 `reasoning_content` 多轮透传，自动适配 `reasoning_effort`。
- **跨会话长期记忆 (Memory Engine)**: ChromaDB 向量库 + SentenceTransformers，结合 LLM 每天定时执行记忆蒸馏复盘。
- **配置驱动的定时任务引擎**: 所有 cron 任务定义在 `config.json`，支持 command/skill 两种类型与 `time_range` 多时段。
- **四通道全覆盖**: 飞书 (WebSocket)、钉钉 (Stream)、企业微信 (HTTP 回调 + pushmsg)，推送 fallback 自动切换。新增 **API 接口 (OpenAI 兼容)**。
- **完美兼容 OpenAI 接口与 Guest 模式**: 原生暴露 `/v1/chat/completions` 等 OpenAI 标准接口，支持 ChatBox、NextChat 等主流第三方客户端无缝对接。带有独立 Guest Token 机制，保障暴露在外网时的安全性。
- **多 Agent 编排复杂任务**: 遇到耗时复杂任务时，后台会自动将请求下发给 TaskOrchestrator，并多线程调度 Planner -> Worker -> Aggregator 的子任务流。
- **RSS 精选引擎**: 多源资讯聚合评分，V2EX 回复数权重加成，低质量标签降权，预计算缓存秒级推送。
- **外部独立监控**: crontab 定时检查 bot 存活，故障时通过企业微信告警，不依赖进程内 `/check`。

## 🛠️ 内置技能库

### 📰 RSS 资讯精选 (`ops_rss.py`)
- **多源聚合**: 量子位、机器之心、虎嗅、36氪、IT之家、V2EX 等，站点权重 + 关键词 + 回复数加权评分。
- **V2EX API 对接**: 调 V2EX API 获取回复数，热门帖自动加分，推广/交易帖自动降权。
- **预计算缓存**: 推送前 13 分钟预计算，HH:03 秒级读缓存发送。

### 💰 财务与账单管理 (`ops_billing.py`)
- **账单解析入库**: 自动从邮箱抓取信用卡账单并落库入账。
- **财务汇总报表**: 一键生成多维度月度/年度账单报表。
- **对账与提醒**: 支持临期还款检查、差异对账、大额交易筛查。

### 🖥️ 系统运维 (`ops_sys.py`, `ops_security.py`, `ops_logs.py`, `ops_self_check.py`)
- **健康自检**: `/check` 一键检查进程、网络、配置、DB、记忆、备份等 9 项指标。
- **安全审查**: 自动扫描 SSH 爆破尝试及异常登录。
- **日志分析**: 跨文件、多关键字高级日志检索。
- **数据备份**: 每天凌晨自动打包备份，`/check` 可查看备份状态。
- **证书监控**: SSL 证书有效期巡检，过期前推送告警。

### 📝 博客管理 (`ops_blog.py`)
- **自动发布与导出**: 结合 Halo API 实现全自动增量博客发布、批量文章导出备份。
- **无感交互**: 与大模型完美融合，只需自然语言即可完成博客素材重组、排版到发布的完整链路。

### ☁️ 云盘灾备 (`ops_bypy.py`)
- **百度网盘直连**: 原生集成 Bypy 客户端，支持网盘容量查询、远程目录管理。
- **自动化增量备份**: 配置深夜 Cron 定时任务，全自动将 Halo 博客数据及 Lite Agent 核心源码增量推送到百度网盘，实现狡兔三窟的数据保障。

### 🎵 媒体与NAS管理 (`ops_media.py`)
- **音乐库整理**: 对接 PostgreSQL 媒体数据库，支持按 FileHash 查重以及查询缺失封面的音乐。
- **全局安全规范**: 媒体库连接使用 `psycopg2` 直连，避免硬编码密码，完全遵守 `AI_GUIDELINES.md`。

## 📦 部署

### 1. 配置
复制 `config.example.json` 为 `config.json`，填入各通道和 LLM 的密钥：

```json
{
    "bot_name": "VPS 助手",
    "project_root": "/root/lite_agent",
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-YOUR_KEY",
        "model": "deepseek-v4-flash"
    },
    "channels": {
        "feishu": { "enabled": true, "app_id": "cli_xxx", "app_secret": "xxx", "admin_open_id": "ou_xxx" },
        "dingtalk": { "enabled": false, "client_id": "xxx", "client_secret": "xxx" },
        "wecom": { "enabled": true, "listen_port": 8899, "push_url": "http://127.0.0.1:6969/send_message", "push_token": "xxx" },
        "api": {
            "enabled": true,
            "host": "0.0.0.0",
            "port": 8887,
            "auth_token": "your_secret_token_here",
            "guest_token": "your_guest_token_here"
        }
    },
    "rssdb": { "uri": "mongodb://user:pass@localhost:27017", "database": "rsslite" },
    "v2ex": { "token": "YOUR_V2EX_TOKEN" },
    "cron_jobs": []
}
```

### 2. 启动
```bash
pip install -r requirements.txt
python3 main.py
```

## 💬 交互指令

| 指令 | 说明 |
|------|------|
| `::rss [ai\|v2ex]` | 查看 RSS 资讯列表 |
| `::rss push` | 手动推送精选简报 |
| `::rss log` | 查看推送/预计算日志 |
| `/check` | 全方位健康自检（进程/网络/备份/DB 等 9 项） |
| `/cron` | 查看定时任务列表 |
| `/cron <序号>` | 手动执行某个定时任务 |
| `/cron log` | 查看定时任务执行日志 |
| `/remember <type> <内容>` | 强制记录长期记忆 |
| `/memory` | 查看记忆池状态 |
| `/balance` | 查询 API 余额 |
| `/status` | 查看会话状态与 Token 消耗 |
| `/history` | 最近对话历史 |
| `/new` | 重置会话 |
| `/help` | 完整帮助 |

## 📄 开源协议

[MIT License](LICENSE)

# Lite Agent

🚀 **Lite Agent** 是一个轻量级、支持深度思考大模型的私有化 AI 智能助手引擎。通过 WebSocket / HTTP 回调 / 长轮询接入**飞书、钉钉、企业微信、Telegram、个人微信 iLink 与内置 Web 控制台**，并通过自然语言全自动调度本地服务器的运维、待办、账单、RSS 精选等 80+ 项技能。

![效果演示](assets/screenshot.png)

## 🌟 核心特性

- **多通道无缝接入**: 一套代码同时支持飞书 (WebSocket)、Telegram (Long Polling/Webhook)、钉钉、企业微信、个人微信 iLink（扫码登录后长轮询）与 Web 控制台。
- **现代化 Web 控制台**: 内置单页 Web Dashboard，集成**邮件中心、BERTopic 热点简报、待办看板、Socks5 代理监控与多通道会话追溯**，支持 SSE 真流式打字渲染与 Guest 访客权限隔离。
- **OpenAI 兼容 API 通道**: 原生提供 `/v1/chat/completions` 等 OpenAI 标准接口，支持流式输出与 Function Calling，可无缝对接 NextChat、ChatBox 等客户端。
- **动态工具分流 (RequestSelector)**: 智能识别意图按需注入领域工具，Token 开销减少 85%+，消除中小模型工具幻觉与误调风险。
- **全自动多模态视觉 (OCR)**: 向机器人直接发送图片，自动调用外置大语言视觉模型（通过 `OCR_ENDPOINT` 代理），秒级解析并返回排版 Markdown。
- **动态技能引擎 (Skills)**: 80+ 项内置运维技能，通过简单 Python 脚本即可快速扩展。
- **复杂任务编排 (DAG)**: 遇到耗时复杂任务时，自动下发给 TaskOrchestrator 调度 Planner -> Worker -> Aggregator 多子任务流。
- **AI 决策委员会**: 支持多大模型（如 GLM-5.3、Kimi-K3 等）协同审议与投票决策。
- **定时推送与长短期记忆**: 支持 Crontab 式定时任务配置与基于向量库的长期记忆蒸馏。
- **独立配置隔离**: 模型与任务路由从配置解耦至 `conf.d/` 独立维护，即改即生效。

## 🛠️ 内置技能概览

- 📰 **RSS 资讯与 BERTopic 热点聚类** (`ops_rss.py`, `scripts/rss_topic/`): 多源资讯聚合评分、BERTopic 主题聚类与宏观热点态势发现、预计算缓存秒级推送。
- 📋 **待办与任务流** (`ops_todo.py`): 待办全生命周期管理（增删改查/延期/搁置）与 IM 每日简报推送。
- 💰 **邮件与财务账单** (`ops_billing.py`, `ops_mail_*.py`): 邮箱账单批量拉取、自动识别入库、多维财务报表与对账。
- 🖥️ **系统运维与安全** (`ops_sys.py`, `ops_sentinel.py`, `ops_security.py`): 主机状态、日志检索、安全哨兵基线与入侵审查、`/check` 一键体检。
- 📝 **博客与网页剪藏** (`ops_blog.py`, `ops_web_clipper.py`): Halo 博客发布与导出、网页 Markdown 提取及 HedgeDoc 自动云端转存。
- ☁️ **云盘灾备** (`ops_backup.py`): 官方 `bdpan` CLI 增量同步，自动打包 Halo、Meilisearch、HedgeDoc、Vaultwarden 等数据至百度网盘。
- 🌐 **网络代理与边缘控制** (`ops_socks5.py`, `ops_edge_*.py`): Socks5 节点测速与动态出站探测、Edge 边缘节点管理。
- 🧠 **AI 决策与学业分析** (`ops_decision.py`, `ops_gaokao.py`): 多模型决策投票、高考志愿位次与专业推荐。

## 📦 部署

### 1. 配置

项目采用 **环境变量分离** 的安全配置方案。

1. 复制 `.env.example` 为 `.env` 并填入密钥：
```bash
cp .env.example .env
vim .env
```

2. 复制配置文件与模型示例配置：
```bash
cp config.example.json config.json

mkdir -p conf.d
cp conf.d/llm.json.example conf.d/llm.json
cp conf.d/task_routing.json.example conf.d/task_routing.json
cp conf.d/committee.json.example conf.d/committee.json
cp conf.d/output_delivery.json.example conf.d/output_delivery.json
cp conf.d/adaptive_execution.json.example conf.d/adaptive_execution.json
```

### 个人微信 iLink（可选）

在 `config.json` 的 `channels` 下开启配置（凭据由扫码生成，不要写进配置文件）：

```bash
# 执行扫码登录（若 systemd 以 liteagent 用户运行，必须以同一用户执行）
sudo -u liteagent -H /usr/bin/python3 -m channels.wechat_ilink --login \
  --session-file data/wechat_session.json
sudo systemctl restart lite-agent
```

### 百度网盘 bdpan 备份（可选）

云盘自动灾备依赖官方 `bdpan` CLI 工具：

```bash
# 方式 A：从百度官方 CDN 下载安装器一键安装 (Linux x86_64 示例)
curl -fsSL https://issuecdn.baidupcs.com/issue/netdisk/ai-bdpan/installer/3.8.4/bdpan-installer-linux-amd64 -o bdpan-installer
chmod +x bdpan-installer
./bdpan-installer --yes
sudo ln -sf ~/.local/bin/bdpan /usr/local/bin/bdpan

# 方式 B：执行 bdpan-storage 官方项目的安装脚本
# bash /path/to/bdpan-storage/skills/baidu-drive/scripts/install.sh --yes

# 授权登录（必须以运行服务的同一用户执行）
sudo -u liteagent -H bdpan login
```

### 2. 启动

```bash
pip install -r requirements.txt
python3 main.py
```

## 💬 常用指令

| 指令 | 说明 |
|------|------|
| `/check` | 全方位健康自检（进程/网络/备份/DB 等 9 项） |
| `/rss_fetch` / `/rss_list` | 获取今日精选 / 查看分类资讯 |
| `/cron` / `/cron log` | 查看定时任务列表 / 执行日志 |
| `/remember <type> <内容>` | 强制写入长期记忆 |
| `/balance` / `/status` | 查询 API 余额 / 会话 Token 统计 |
| `/new` / `/help` | 重置当前会话 / 完整帮助 |

## 📄 开源协议

[MIT License](LICENSE)

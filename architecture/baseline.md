# Lite Agent 架构重构基线状态记录 (Phase 1 Baseline)

- 编制日期：2026-10-15
- 实施分支：`codex/kernel-boundaries`
- 基准分支：`main`

---

## 1. 仓库与版本快照

| 仓库 | 路径 | Commit SHA | 分支 | 状态 |
|---|---|---|---|---|
| **Agent** | `/Users/lilee/Projects/lite_agent` | `fd17d67572be0627e144e6846c24400e4c9257a8` | `main` | 干净工作区 (Clean) |
| **Parser** | `/Users/lilee/Projects/dev/mail-statement-parser` | `918bce6b6d80dbf6d2af61cac35cbe24e44a7cdc` | `master` | 干净工作区 (Clean) |

- **执行运行环境**: Python 3.13.5 (macOS aarch64)
- **生产兼容环境**: Python 3.9.2 (Debian / Linux)
- **容器测试镜像**: `lite_agent_test:3.9` (Python 3.9-slim)

---

## 2. 门禁与测试基线数据

### 2.1 静态架构门禁 (`scripts/check_architecture.py`)
- **扫描 Python 文件数**: 206 个
- **未登记违规项 (Violations)**: 0
- **已核准例外项 (Approved Exceptions)**: 23 处（登记于 `architecture_rules.json`）
  - core 运行时反向依赖例外: 6 处（会议指令 3 处、会议通知 1 处、会议 API 1 处、任务编排器直接导入 BillingWorkflow 1 处）
  - core 输出与技能加载动态导入/路径例外: 7 处（`core/output_delivery.py` 4 处、`core/skill_engine.py` 3 处）
  - main.py 启动动态导入/路径例外: 2 处
  - 测试套件受控跨仓例外: 8 处（`tests/test_mail_quality_closeout.py` 4 处、`tests/test_mail_r6_closeout.py` 4 处）

### 2.2 测试套件实际运行统计
- **本机环境 (Python 3.13.5)**:
  - Agent Pytest: `910 passed, 4 subtests passed in 49.74s`
  - Parser Unittest: `Ran 205 tests in 2.118s OK`
- **容器隔离环境 (Python 3.9 Docker Snapshot, :ro)**:
  - Parser Unittest: `205 tests passed`
  - AST 架构门禁: `PASS (23 approved exceptions)`
  - Agent Pytest: `910 passed, 4 subtests passed`

---

## 3. 调用链与启动副作用分析

### 3.1 `main.py` 启动副作用
- 模块顶层即添加 `PROJECT_ROOT` 到 `sys.path`。
- 直接根据业务名称导入并注册后台 cron 作业（账单同步、Todo 检查、备份任务等）。
- 存在向邮件技能注入全局 Agent 的副作用（`skills.ops_mail_reader._agent = agent`），导致技能直接持有全局单例运行时。

### 3.2 `agent.py` 启动与运行时耦合
- 导入即修改全局状态：`from core.command_registry import _registry` 并在模块加载时执行 `_registry.register('/new', ...)`。
- 直接定义了公共传输模型 `IncomingMessage` 与 `AgentResponse`，使外层 channels 必须导入完整的 `agent.py`，无法单独依赖消息契约。
- 默认实例化 `SkillEngine`，其扫描 `skills/` 目录加载全部 Python 文件，引发模块副作用与全局 `_skill_registry` 污染。
- 通用任务编排器 `core/task_orchestrator.py` 在初始化时直接实例化 `BillingWorkflow`。

### 3.3 模块所有者与分层归属划分
- **基础内核 (Kernel Core)**: `core/`, `contracts/`, `services/`, `app/`, `agent.py` (通用运行时部分), `session.py`, `security.py`。
- **业务特性 (Features)**: `features/billing/`, `features/todo/`, 仅 billing/todo 当前存在；review_meeting/mail 是阶段二目标目录。
- **协议适配 (Integrations)**: `integrations/mail/` (Parser CLI 桥接), `integrations/smtp/` (平台邮件客户端), `integrations/notifications.py` 等。
- **宿主与适配通道 (Channels)**: `channels/api.py`, `channels/feishu.py`, `channels/dingtalk.py` 等。
- **薄入口与工具 (Skills & Scripts)**: `skills/` (保留参数与权限装饰器), `scripts/` (独立命令行维护工具)。

---

## 4. 现有配置体系与加载优先级（源码复核修正）

- `main.py:load_config` 先检查项目根 `config.json` 是否存在，再委托 `core.config_loader.load_config`；当前这一步仍有固定根路径约束，阶段二必须处理。
- `core/config_loader.py:_get_base_dir` 使用 `LITE_AGENT_CONFIG_ROOT`（测试覆盖的 PROJECT_ROOT 另有优先规则）。`LITE_AGENT_CONFIG_FILE` 可指定配置文件；不是 `LITE_AGENT_CONFIG`。
- 基础配置载入后，按 `conf.d/*.json` 的模块名深合并；随后清除 edge 键并展开受信配置的 `${ENV}`。
- `load_env` 从配置根 `.env` 加载值，TESTING/disable dotenv 时跳过；调用时机和环境覆盖语义必须随阶段二启动装配核对，不概括成“环境变量总是优先”。
- `load_config` 最后合并 SQLite settings 覆盖，有敏感键/edge 限制、不对 SQLite 值做变量展开，存在缓存 TTL。原报告遗漏此配置来源。
- Parser 路径另由 mail paths / Parser 自身环境变量解析，不能混作 Agent 主配置文件路径。

## 5. 公开调用面：源码入口与待完成盘点

本阶段不删除公开入口。下列是真实路径样例，不是完整兼容清单：

| 入口 | 当前源码位置 | 已核实路径样例 |
|---|---|---|
| 通用 HTTP | channels/api.py:do_GET/do_POST | POST /api/v1/chat、/api/v1/task、/v1/chat/completions；GET /api/v1/task/stream、/api/v1/sessions、/v1/models |
| 业务 HTTP | channels/api.py | /api/v1/todos、/api/v1/rss/brief、/api/v1/review-meetings 路由组；会议 ID/action 使用正则路由 |
| 系统命令 | agent.py:_handle_builtin、core/command_registry.py | /new 等，仍有内置分支和装饰器注册 |
| 维护脚本 | scripts/ | verify_e0.py、check_architecture.py、review_meeting.py |

原交付中 `/api/chat`、`/api/health`、`/api/review-meeting/poll` 及 `scripts/daily_briefing.py` 等列举未与源码一致，已撤下。阶段二迁移前必须从路由条件、注册器、shell/bat、CLI help 和测试汇总完整命令/参数/URL清单，包含正则与动态注册，不能拿本表样例当全集；阶段一没有删除接口，因此此待核验项不影响当前契约迁移。

## 6. 外部调用面与生产待核验项

> [!NOTE]
> 实施 AI 无生产 VPS 访问凭据，以下内容待复核方（Codex / 用户）在发布时集中核验：

1. **Systemd 服务**: VPS 上的 `/etc/systemd/system/lite-agent.service` 是否包含特定的环境变量、工作目录或 Python 解释器路径。
2. **生产 Crontab**: VPS `liteagent` 用户 crontab 中是否有定时执行 `scripts/*.py` 的条目。
3. **外部 Webhook / 回调地址**: 飞书、钉钉、企业微信开发平台后台配置的 Webhook 回调 URL 是否稳定映射到 `/webhook/*` 与 `/api/*`。

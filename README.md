# Lite Agent

个人 AI 助手：以统一的模型调用、会话、权限和工具执行内核，连接飞书、Telegram、钉钉、企业微信、微信 iLink 与 HTTP API。业务由显式插件装配，支持待办、会议、邮件账单、内容检索及系统运维。

## 目录与边界

```text
main.py                 保留的生产启动入口，委托 lite_agent.cli，支持 core / full

lite_agent/             运行时包
  agent.py              宿主、消息处理与显式依赖注入
  session.py            会话与消息状态
  security.py           权限与路径预检查
  cli.py                命令行解析、模式与插件装配
  app/                  应用装配、插件清单与生命周期
  contracts/            纯数据契约
  core/                 模型调用、执行、路由与编排
  services/             通用注册表、调度和投递
  channels/             通信适配器
  features/             八个业务插件及规范服务
  skills/               工具元数据与注册载体
  integrations/         SMTP、通知、邮件解析等外部集成
  memory_engine/        长期记忆

dev/                    开发资产，生产运行不依赖
  config/               架构规则、示例、pytest 与容器配置
  requirements/         core / test / full 依赖
  tests/                按职责归类的自动化测试

edge_node/              边缘节点组件
web_dashboard/          Web 控制台
app/android/            Android 客户端
scripts/                运维与验证入口；rss_topic 为外部任务链
assets/                 图片与许可证
conf.d/                 配置分片示例；私有 JSON 不入库
```

内核不得反向依赖业务插件；业务由 `lite_agent.app` 显式加载。`lite_agent/skills/` 中的装饰器在导入时即注册工具，属载荷性导入，不能按静态扫描的“未使用”结果删除。架构规则在 `dev/config/architecture/rules.json`，核心导出清单在 `dev/config/architecture/core-manifest.txt`。

规范导入入口为 `lite_agent.contracts.execution`、`lite_agent.contracts.result`、`lite_agent.services.commands` 和 `lite_agent.services.cron`。邮件质量事件与同步编排直接使用 `lite_agent.features.billing.quality_todo`、`lite_agent.features.billing.sync_service`；`lite_agent.integrations.mail` 保留外部 Parser CLI、路径与进程适配。根目录 `agent.py` / `session.py` / `security.py`、对应 core 转发模块、邮件兼容层及 `core.cron_engine` 已删除，不再作为导入入口。`main.py`、技能注册和 `scripts/review_meeting.py` 等 CLI 薄入口继续保留。

历史设计与复核材料不入库；运行数据、密钥、`.env` 和私有配置不提交。

## 安装与配置

运行与 CI 基线为 **Python 3.13**。独立安装解释器并创建项目虚拟环境，不替换宿主 `/usr/bin/python3`，不复用其他项目的全局依赖。首次安装时在仓库根目录执行；已有部署不要覆盖配置：

```bash
python3.13 -m venv .venv313
source .venv313/bin/activate
python -m pip install -r dev/requirements/core.txt -r dev/requirements/full.txt
cp dev/config/examples/env.example .env
cp dev/config/examples/agent.json config.json
mkdir -p conf.d
cp conf.d/llm.json.example conf.d/llm.json
cp conf.d/task_routing.json.example conf.d/task_routing.json
cp conf.d/committee.json.example conf.d/committee.json
cp conf.d/output_delivery.json.example conf.d/output_delivery.json
cp conf.d/adaptive_execution.json.example conf.d/adaptive_execution.json
cp conf.d/task_specs.json.example conf.d/task_specs.json
```

填写自己的模型、频道管理员身份及业务配置，保护配置文件权限。根目录 `config.json`、`.env` 与 `conf.d/*.json` 保持原位置。纯 core 环境可只安装 `dev/requirements/core.txt`，并使用关闭业务通道与插件的配置；默认示例面向 full 部署。

```bash
python main.py --mode full --config config.json
python main.py --mode core --config config.json
```

full 模式默认装配 billing、todo、review_meeting、mail_ops、sys_ops、network_ops、content_ops、phone_app。可用 `--plugins todo,review_meeting` 显式选取；CLI 插件列表优先于 `LITE_AGENT_PLUGINS` 和 `plugins.enabled`，`plugins.disabled` 仍会过滤。core 模式不装配业务插件。启用插件前核对其定时任务、后台 Worker 和外发目标。

邮件账单依赖外部 `mail-statement-parser` 项目，通过 `billing.script_dir` 或 `MAIL_STATEMENT_PARSER_DIR` 指定目录。解释器优先级为 `MAIL_STATEMENT_PARSER_PYTHON` → `billing.python_executable` → Parser 的 `.venv/bin/python` → `/usr/bin/python3` → 当前进程解释器。显式配置路径不存在或不可执行时直接报错，不静默回退。账单库与待办库分别通过 `billing.statement_db_path`、`billing.todo_db_path` 配置，不应混用。生产运行基线、依赖版本清单及回滚范围见 [VPS1 运行说明](dev/runtime_notes/vps1-python313-runtime.md)。

微信 iLink 登录必须使用运行服务的同一系统用户；账号状态写在自己的 `data/` 中：

```bash
python3 -m lite_agent.channels.wechat_ilink --login --session-file data/wechat_session.json
```

## 开发验证

```bash
python -m pip install -r dev/requirements/test.txt
python -m pytest -c dev/config/pytest.ini --rootdir=. dev/tests/ -p no:cacheprovider -p dev.tests.support.no_skips
python scripts/check_architecture.py
python scripts/verify.py --parser-root /path/to/mail-statement-parser
```

完整验证需要 Node.js、Docker 和固定版本的 Parser checkout；当前跨仓基线为 `fa3600d813d9b0b07171baf7c8ab9aeebc77d17d`，以 CI 与隔离守卫中的声明为准。`verify.py` 的 Parser 默认位置为相邻 `../dev/mail-statement-parser`，可用 `--parser-root PATH` 覆盖。

五道 CI Gate 分别验证独立 Core、前端 Node 契约、Agent 全量回归、固定 Parser 跨仓契约和 Docker **Python 3.13** 脱敏只读双回归。测试使用隔离配置、临时数据库与脱敏快照，禁止跳过用例冒充通过。Docker 快照不包含 `.git`，由宿主在构建快照时写入真实 Parser SHA 元数据供版本比对；测试镜像使用仅含 Dockerfile 和测试依赖的构建上下文，不把私有配置交给 Docker builder。缺少 Docker 时的 PENDING 或显式 `--skip-docker` 不等同于完整门禁通过。

测试路径基于仓库位置解析，可从仓外执行，不需要全局设置 `PYTHONPATH`：

```bash
cd /tmp
/path/to/lite_agent/.venv313/bin/python -m pytest /path/to/lite_agent/dev/tests -q -p no:cacheprovider -p dev.tests.support.no_skips
```

```bash
python scripts/verify.py --core-only
python scripts/verify_core.py
python scripts/export_core.py --verify-only --check-hashes
```

测试分组：`kernel/` 内核，`architecture/` 分层与隔离，`channels/` 频道，`features/` 业务，`integration/` 跨模块与 Parser 合同，`regression/` 长期反例，`tooling/` 构建与验证工具，`frontend/` 前端契约；`support/` 为隔离守卫，`fixtures/` 为明确脱敏的机器输入。

## 调度器与关闭语义

`create_app()` 为每个应用创建专属 `CronService`，Agent、插件、配置任务、`/cron` 与健康探针使用同一实例。裸 `Agent(cron=None)` 才使用 `services.cron.default_cron_service` 兜底；显式注入的调度器（包括 falsy 对象）不会被记忆命令初始化覆盖。技能导入不启动调度循环，CLI 在插件与配置任务注册完成后启动应用调度器。

自动任务由独立 `CronEngine` 后台线程提交到工作线程池，默认 5 个工作线程；`start()` 不等待任务执行完毕。`/cron <编号>` 手动执行仍同步返回，并与自动执行共用任务互斥。配置注册同名同定义时保留原任务对象与分钟状态，同名不同定义报错；空时间槽、非法范围等错误阻止启动，不静默遗漏任务。

应用关闭先拒绝新执行，等待自动和手动在途任务完成，再释放插件与其他资源。排空期间调用 `start()` 会报错；停止完成、旧循环退出且在途任务排空后才允许重启，旧执行器的延迟回调不能进入新一轮运行。`Application.shutdown()` 使用无限等待，长期阻塞的业务任务会延迟关闭；`stop(timeout=...)` 也不是业务任务的强制终止机制。健康探针的 `busy_workers` 只统计线程池任务，`longest_job` 包含手动执行。

调度器仅保存内存状态，不补跑停机期间错过的时间槽，也不保证跨重启恰好执行一次。备份等关键任务若需要补偿执行，应另外设计持久回执和幂等策略。时间表达式按服务进程本地时区解释，部署时需核对时区。

## 权限与工作区

通道按管理员身份区分访客，命令权限与技能执行上下文继续限制可用操作；Cron 重构不会授予系统权限。定时任务由可信配置和插件登记，并以服务账户权限执行，不是每次经过聊天授权的请求。

`ops_workspace_run` 默认不向访客开放，脚本写入 `workspace/` 并在该目录执行，包含超时和特权命令拦截。但工作目录和 Python 审计探针不构成操作系统沙箱：脚本仍继承服务账户的文件、网络权限与进程环境。路径预检查只对实际调用该检查的入口生效。不要将其理解为“只能读写 workspace”，也不要给生成脚本配置通用 root/sudo 权限。

## 运维入口

| 入口 | 用途 |
| --- | --- |
| `scripts/check_models.py` | 模型配置检查 |
| `scripts/debug_api.py` | 明确目标后的 API 手工诊断 |
| `scripts/review_meeting.py` | 会审 CLI 薄入口 |
| `scripts/sync_web_static.sh` | 本地或 SSH 远端发布完整 Web 资源并生成内容指纹 |
| `scripts/build_web_assets.py` | 构建 JS/CSS 内容哈希、HTML 版本徽章和 build-sha |
| `scripts/run_decision.py` | 现有决策 CLI |
| `scripts/gen_edge_keys.py`、`scripts/gen_htpasswd.py` | 边缘密钥及认证文件生成 |
| `scripts/switch_model.py` | 模型配置切换 |
| `scripts/switch_naive_config.sh` | 已有受限代理配置切换脚本 |
| `scripts/backup_to_bypy.sh`、`full_backup_once.py`、`rsslite_full_backup.py` | 既有备份入口，执行前确认目标和权限 |
| `scripts/rss_topic/` | 既有 RSS Topic 外部流水线，保留路径以兼容 cron |

运维入口可能发送通知、修改外部系统或备份数据，不作为普通单元测试直接运行。生产配置、数据库和微信状态不能被测试副本覆盖；回退代码时保留上线后新增的业务状态。

Web 发布示例：

```bash
./scripts/sync_web_static.sh /path/to/nginx/dist
./scripts/sync_web_static.sh --remote vps1
```

默认本地目标是 `/var/www/dashboard/dist`；远端模式先在本机构建，再同步到脚本指定的 VPS 路径，需要对应 SSH 和目标目录权限。发布前确认工作区内容与构建 SHA 对应；脚本直接同步目标目录，不提供原子切换或自动回滚。页面保留 Web 构建 SHA 并与服务端 SHA 比较，不一致时显示双方版本。`/health` 和 `/api/v1/health` 返回服务端构建信息及 Cron 存活状态；经反向代理可映射到 `/agent/health`。

许可证见 [assets/LICENSE](assets/LICENSE)。

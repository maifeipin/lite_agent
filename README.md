# Lite Agent

个人 AI 助手：以统一的模型调用、会话、权限和工具执行内核，连接飞书、Telegram、钉钉、企业微信、微信 iLink 与 HTTP API。业务由显式插件装配，支持待办、会议、邮件账单、内容检索及系统运维。

## 目录与边界

```text
main.py                 唯一启动入口，支持 core / full
agent.py                Agent 宿主与消息处理
session.py              会话与消息状态
security.py             权限与安全检查
app/                    应用装配、插件清单与生命周期
contracts/              纯数据契约
core/                   模型调用、执行、路由与编排
services/               通用注册表、调度和投递
channels/               通信适配器
features/               七个业务插件及规范服务
skills/                 工具元数据与现有兼容入口
integrations/           SMTP、通知、邮件解析等外部集成
memory_engine/          长期记忆
edge_node/              边缘节点组件
web_dashboard/          Web 控制台
config/                 架构规则、示例、测试与容器配置
requirements/           core / test / full 依赖
scripts/                当前运维与验证入口；rss_topic 为外部任务链
rules/                  业务规则
assets/                 图片与许可证
conf.d/                 配置分片示例；私有 JSON 不入库
tests/                  按职责归类的自动化测试
```

内核不得反向依赖业务插件；业务由 `app` 显式加载。`skills/` 中的装饰器与薄入口仍有实际消费者，不按“未使用导入”机械删除。架构规则在 `config/architecture/rules.json`，核心导出清单在 `config/architecture/core-manifest.txt`。

历史设计、复核报告、执行日志与旧手工验证脚本保存在本机 `.local/archive/`，不进入后续提交；`architecture/`、`docs/` 也已忽略。运行数据、密钥、`.env` 和私有配置不提交。历史材料从当前树退出不等于清除了 Git 历史。

## 安装与配置

在仓库根目录执行：

```bash
python3 -m pip install -r requirements/full.txt
cp config/examples/env.example .env
cp config/examples/agent.json config.json
mkdir -p conf.d
cp conf.d/llm.json.example conf.d/llm.json
cp conf.d/task_routing.json.example conf.d/task_routing.json
cp conf.d/committee.json.example conf.d/committee.json
cp conf.d/output_delivery.json.example conf.d/output_delivery.json
cp conf.d/adaptive_execution.json.example conf.d/adaptive_execution.json
cp conf.d/task_specs.json.example conf.d/task_specs.json
```

填写自己的模型、频道及业务配置，保护配置文件权限。现有部署的根目录 `config.json`、`.env` 与 `conf.d/*.json` 保持原位置；本轮目录整理没有移动生产配置或运行数据。

```bash
python3 main.py --mode full --config config.json
python3 main.py --mode core --config config.json
```

full 模式默认装配 billing、todo、review_meeting、mail_ops、sys_ops、network_ops、content_ops。可用 `--plugins todo,review_meeting` 显式选取；CLI 插件列表优先于 `LITE_AGENT_PLUGINS` 和 `plugins.enabled`，`plugins.disabled` 仍会过滤。core 模式不装配业务插件。启用插件前核对其定时任务、后台 Worker 和外发目标。

微信 iLink 登录必须使用运行服务的同一系统用户；账号状态写在自己的 `data/` 中：

```bash
python3 -m channels.wechat_ilink --login --session-file data/wechat_session.json
```

## 开发验证

```bash
python3 -m pip install -r dev/requirements/test.txt
python3 -m pytest -c dev/config/pytest.ini --rootdir=. dev/tests/ -p dev.tests.support.no_skips
python3 scripts/check_architecture.py
python3 scripts/verify.py
```

`verify.py` 执行本机、Node 及 Docker Python 3.9 门禁；Parser 默认取相邻 `../dev/mail-statement-parser`，也可用 `--parser-root PATH`。测试使用隔离配置、临时数据库与脱敏快照，禁止跳过用例冒充通过。完整测试镜像由脚本在仅含 Dockerfile 和测试依赖的隔离构建上下文生成，不把私有配置交给 Docker builder。

```bash
python3 scripts/verify.py --core-only
python3 scripts/verify_core.py
python3 scripts/export_core.py --verify-only --check-hashes
```

测试分组：`kernel/` 内核，`architecture/` 分层与隔离，`channels/` 频道，`features/` 业务，`integration/` 跨模块与 Parser 合同，`regression/` 长期反例，`tooling/` 构建与验证工具，`frontend/` 前端契约；`support/` 为隔离守卫，`fixtures/` 为明确脱敏的机器输入。

## 运维入口

| 入口 | 用途 |
| --- | --- |
| `scripts/check_models.py` | 模型配置检查 |
| `scripts/debug_api.py` | 明确目标后的 API 手工诊断 |
| `scripts/review_meeting.py` | 会审 CLI 薄入口 |
| `scripts/run_decision.py` | 现有决策 CLI |
| `scripts/gen_edge_keys.py`、`scripts/gen_htpasswd.py` | 边缘密钥及认证文件生成 |
| `scripts/switch_model.py` | 模型配置切换 |
| `scripts/switch_naive_config.sh` | 已有受限代理配置切换脚本 |
| `scripts/backup_to_bypy.sh`、`full_backup_once.py`、`rsslite_full_backup.py` | 既有备份入口，执行前确认目标和权限 |
| `scripts/rss_topic/` | 既有 RSS Topic 外部流水线，保留路径以兼容 cron |

运维入口可能发送通知、修改外部系统或备份数据，不作为普通单元测试直接运行。生产配置、数据库和微信状态不能被测试副本覆盖；回退代码时保留上线后新增的业务状态。

许可证见 [assets/LICENSE](assets/LICENSE)。

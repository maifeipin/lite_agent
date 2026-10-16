# SMTP 邮件发送配置映射与去 Parser 迁移对照表 (SMTP Config Mapping)

- 编制日期：2026-10-15
- 实施角色：Antigravity (实施 AI)
- 文档性质：**受版本跟踪的长期架构契约与配置对照基准**
- 目标：**解除平台级消息外发对外部兄弟仓 `mail-statement-parser` 的代码借用与路径污染，实现基于标准库与独立配置的自主 SMTP 外发能力。**

---

## 1. 背景与改造目标

- **历史问题**：
  原 `core/output_delivery.py` 在执行外部长文本邮件投递时，动态将 `config["billing"]["script_dir"]` (即外部 Parser 路径) 插入 `sys.path`，并动态加载 Parser 的 `mail_client` 与 `mail_connect` 读取外部账号配置和建立连接。
  这导致：
  1. 基础内核在运行时产生对 Parser 物理路径的强依赖，违反无外部依赖独立运行原则；
  2. 篡改进程级 `sys.path` 与全局 `CONFIG_CANDIDATES`，产生隐蔽线程与路径副作用；
  3. 当 Parser 不存在或未配置时，即使有可用 SMTP 也无法进行平台投递。
- **阶段二新设计**：
  在 `integrations/smtp/` 实现标准邮件协议发送器：
  1. 纯基于 Python 标准库 `smtplib` 与 `email.mime`；
  2. 优先读取 Lite Agent 原生 `config.json` 的 `smtp` 或 `output_delivery.email` 配置；
  3. **严禁导入任何 Parser 模块，严禁修改 `sys.path`**；
  4. 支持注入测试连接器，保障离线与无真实收件人验证。

---

## 2. 配置字段映射对照表

| 配置项语义 | 旧配置来源 (依赖 Parser) | 新原生配置项 (独立配置) | 环境变量覆盖 | 默认值 | 缺失时的行为 / 错误 |
|---|---|---|---|---|---|
| **邮件投递启用开关** | `output_delivery.email.enabled` | `output_delivery.email.enabled` 或 `smtp.enabled` | `LITE_AGENT_SMTP_ENABLED` | `false` | 若为 false 则不触发邮件投递，安全跳过 |
| **邮件收件人** | `output_delivery.email.recipient` | `output_delivery.email.recipient` 或 `smtp.recipient` | `LITE_AGENT_SMTP_RECIPIENT` | `""` | 显式要求 email 投递但无收件人时：真实报错并记录投递失败 |
| **SMTP 主机地址** | 依赖 Parser `accounts.json` 的 `host` | `smtp.host` 或 `output_delivery.email.host` | `LITE_AGENT_SMTP_HOST` | `""` | 缺少主机时连接报错，不假冒发送成功 |
| **SMTP 端口号** | 依赖 Parser `accounts.json` 的 `port` | `smtp.port` 或 `output_delivery.email.port` | `LITE_AGENT_SMTP_PORT` | `465` (SSL) 或 `587` (TLS) | 按端口类型自动识别安全模式 |
| **发件人账户 (User)** | 依赖 Parser `accounts.json` 的 `account` | `smtp.username` 或 `output_delivery.email.username` | `LITE_AGENT_SMTP_USER` | `""` | 缺少发件人时拒绝发送 |
| **发件人授权码/密码** | 依赖 Parser `accounts.json` 的 `password` | `smtp.password` 或 `output_delivery.email.password` | `LITE_AGENT_SMTP_PASSWORD` | `""` | 仅读取环境变量或脱敏配置，严禁打印/入日志 |
| **SSL / TLS 模式** | 依赖 Parser `mail_connect` | `smtp.use_ssl` (bool) / `smtp.use_tls` (bool) | `LITE_AGENT_SMTP_USE_SSL` | 端口 465 默认为 `true` | 标准 SSL/STARTTLS 协商 |
| **邮件主题前缀** | `output_delivery.email.subject_prefix` | `output_delivery.email.subject_prefix` | `LITE_AGENT_EMAIL_PREFIX` | `"[Lite Agent 完整回复]"` | 自动附加任务标题 |
| **多账户轮询支持** | 依赖 Parser 动态 load_accounts | `smtp.accounts` 列表 (支持兼容格式) | 无 | `[]` | 若配置多账号，按配置顺序故障自动回退 |

---

## 3. 投递行为与安全不变量

1. **单次投递不变量 (Once-only)**：
   一次执行请求或工作流至多成功发送一次外发邮件；成功建立并投递后立即生成最终回执，绝不因为后续 teardown 异常重复向其它账号投递。
2. **零外发安全保护**：
   - 当 `full_delivery: "inline"` 或 `"sqlite"` 时，**严禁触发任何 SMTP 网络连接**；
   - 单元测试与 CI 运行必须使用 Fake/Mock SMTP 注入，严禁配置生产真实收件人邮箱；
   - 显式配置 `hedgedoc` 投递失败时，遵守 `allow_email_fallback` 策略，不得私自向外部邮箱外泄机密。
3. **真实失败不变量**：
   若无可用 SMTP 连接或认证失败，投递状态必须真实标记为失败 (`status='partial'` / `failed`)，**严禁伪造成功回执**。
4. **Parser 完全解耦不变量**：
   当环境中物理删除外部 `mail-statement-parser` 仓库或设置非法目录时，系统只要原生配置有效，依然能够正常发送邮件，不受任何阻断。

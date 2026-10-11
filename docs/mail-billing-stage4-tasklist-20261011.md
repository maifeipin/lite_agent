# 第四阶段执行清单：同步后的质量事件自动消费

前置：R1–R8 三阶段已本地验收。本阶段只打通现有同步入口与已实现的 quality_todo；达到约 90% 后交付 diff、测试和剩余边界，由复核者直接收尾。不 push、不部署、不改生产 cron、不访问真实邮箱、不发外部通知。

## 已核实的现状

- core/billing_workflow.py 的 fetch() 执行一次 sync3m，消费 stdout 中 JSON_SUMMARY，目前没有调用 quality_todo。
- skills/ops_billing.py 的 billing_fetch() 直接执行 sync3m；_billing_fetch_cron() 每日 03:00 调用它，只打印日志。已有 CronManager，勿再创建一套并行邮件轮询器。
- integrations/mail/client.py 支持显式 script_dir、db_path，子进程可通过 MAIL_STATEMENT_DB_PATH 指向同一库；quality_events() 在 SQL 限流前过滤质量事件。
- integrations/mail/quality_todo.py 的 consume_quality_events() 已有条件 ack、错误统计、幂等 Todo、人工状态保护及解决事件分流。statement_recorded 留给专门的还款适配器。
- core/billing_snapshot.py 仍是旧本地读模型。目录迁移与统一 CLI 读模型另分一阶段，勿在本阶段混入。

## 文件与接口

1. 新建 integrations/mail/sync_receipt.py：将 _parse_json_summary 和账户状态校验抽为纯函数。保留 core/billing_workflow.py 原函数作为兼容导出，原有调用/测试不破坏。不得因脱敏损坏 JSON，不输出 token/密码；无回执、坏回执、非零进程必须分别保留失败原因。
2. 新建 integrations/mail/sync_service.py：统一运行一次 sync3m，并在本次拉取后独立、有上限地消费已落库的质量事件。接口 sync_and_consume(script_dir, statement_db_path, todo_db_path, months=1, event_limit=100, max_batches=3)，返回 {fetch_log, fetch_errors, quality_stats, partial}。timeout 保留 300 秒外层兜底，账户内预算沿用 parser；使用同一个配置库路径执行 fetch、读事件及 ack。
3. 修改 core/billing_workflow.py：fetch() 委托服务，兼容既有 (log, errors) 返回；quality 消费失败进入错误列表及报告，不能标全成功，也不能掩盖已经成功拉取的账户。显式传入配置的 statement_db_path；Todo 路径未配置时使用 ops_todo 的默认路径，不从邮件库目录推测。
4. 修改 skills/ops_billing.py：billing_fetch() 调用同一服务，文字返回区分邮箱拉取和 Todo 同步结果。03:00 cron 复用此路径，不增加第二次全盘邮箱扫描、不新注册重复抓取任务。
5. MailCLI 或服务新增按需配置支持，只传目录/库路径，不读取/复制邮箱认证信息。sync3m 的 tagged summary 与普通 JSON Envelope 分开处理，不把二者当成同一协议。

## 失败与成本规则

- 某邮箱失败或 fetch 超时后，已经提交的质量事件仍允许独立消费；抓取失败继续报告。不要因为 Todo 失败重复整批 fetch。
- 默认每轮 100 个事件、至多 3 轮。遇到没有 ack 进展/写入失败/陈旧 revision，停止本次 drain，保留待重试事件，禁止反复读取同批失败事件形成忙循环。
- 持续积压需输出 backlog/上限命中，不称全部处理完成。不要全量原文写日志/模型。
- 此阶段不新增 LLM 调用；同步、诊断、Todo 都走确定性逻辑。复用 parser 租约和 Todo 并发保护，不绕过人工编辑及 tombstone。
- 跳过 statement_recorded，不能用质量消费者 ack 新账单事件。仍以 revision 条件 ack；写 Todo 成功、ack 失败可重投并保持一个 Todo。

## 必须验收

- 用临时 parser DB + Todo DB，通过真实 CLI 完成 sync 服务后质量事件创建、重投不重复、解决关闭；sync 外部连接用 stub/离线消息，不访问真实邮箱。
- 手动 skill、工作流、cron 共享同一服务、同一 configured DB；同 UID 在另一邮箱不能误关联。
- 全部/部分账户失败、无回执、坏 JSON、timeout、Todo 写失败、ack 陈旧、积压超过批次上限都有独立断言；写失败不 ack、不触发第二次 fetch。
- 先以本地 stub 跑各入口，再运行两仓完整回归、Python 3.9 语法检查及 diff --check。交付测试数量与未实现项，不用“全部完成”涵盖后续提醒。

## 后续单独阶段

- statement_recorded → 按邮箱/银行/Message-ID 的出账 Todo；依据 repayment_reminder Gate、独立币种、is_paid 与人工状态提供临期提醒。需要专属事件过滤/防饥饿、幂等发送回执和还款核销设计，不能在本阶段顺手实现。
- 普通邮件的关键信息缺失/格式变化事件，以及普通邮件 Todo 生命周期。
- 账单能力迁出 core、以共享 CLI 读模型替代重复 SQL，保留兼容导出并避免两套业务统计。
- Gmail 认证及真实模板样例采集。
- 上述本地验证完成后另行准备 VPS 发布、备份/回滚及生产用例验收，不把本阶段测试当成生产部署证明。

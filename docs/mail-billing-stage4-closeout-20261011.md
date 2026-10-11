# 第四阶段收尾验收：同步后的质量事件自动消费

日期：2026-10-11。状态：本地实现及独立复核完成；未推送、未部署、未访问真实邮箱、未发送外部通知。本文替换执行 AI 的 90% 交接报告。

## 最终行为

手动 billing_fetch、BillingWorkflow.fetch 和既有 03:00 cron 共用 sync_and_consume：仅运行一次 sync3m，随后消费已落库的质量事件。fetch、读取事件及条件 ack 使用同一个显式邮件库；Todo 使用配置 todo_db_path 或既有默认路径。抓取失败仍允许独立消费既有事件，Todo 失败不重新抓取。

质量事件每批最多 100 条、最多 3 批，每次服务消费阶段采用 30 秒预算。CLI 读取和 ack 使用剩余时间；事件间检查预算，耗尽后停止并保留未确认事件。预算为协作式边界：已经开始的 SQLite/Todo 操作可能在预算后完成，随后不再 ack 或启动下一事件。该阶段不新增 LLM 调用。

质量事件严格分流，statement_recorded 留给专属还款适配器；解决事件关闭对应质量 Todo。人工状态、描述、日期及 tombstone 继续使用既有保护机制，ack 继续携带 revision。

## 独立复核补齐

1. 删除 TypeError 后不带 env 再次执行 runner 的重试：避免重复抓取及写入错误数据库。
2. 工作流和技能显式选择各自邮件库默认值；技能转交配置 Todo 路径，拒绝注入与显式库不一致的 MailCLI。
3. 拉取事件失败不能当作空队列；积压探测失败、无确认进展和上限命中均不标记完成。短批次结束也探测剩余事件。
4. 首个写入或 ack 错误在批次内部停止，配合 30 秒预算，避免继续处理后续事件放大成本。
5. 仅末尾回执有效，末尾截断不能复用前面的成功回执；非整数、负数及布尔型计数均拒绝。
6. 对 JSON 对象递归脱敏后序列化，保护带转义引号的密码；损坏/截断的回执内容从日志移除，账户标识不影响状态判断。
7. 技能使用 parser 的 new_bills 计数；空事件队列明确不代表既有 Todo 已解决；工作流将同步/质量问题纳入报告错误。
8. 原新增测试存在默认 Todo 库写入风险，已加入临时库隔离。新增独立复现测试覆盖上述边界，含 fetch stub 实际写入配置临时库、真实 CLI 消费及 ack。

## 验证

- 针对同步服务、独立边界及账单工作流：79 项通过。
- parser 全量 unittest：85 项通过。parser 本阶段未修改。
- Agent 全量 pytest：697 项通过，4 个子测试通过（38.89 秒，含 switch_model 既有回归）。
- 8 个修改/新增 Python 文件通过 Python 3.9 AST 语法检查及内存编译；实际回归运行环境为 Python 3.13。
- git diff --check 通过。
- 测试使用临时邮件/Todo 数据库、离线事件及 fetch stub；不以本地测试宣称生产验收。

复现命令：

```sh
# lite_agent
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m pytest -q -p no:cacheprovider --tb=short
# mail-statement-parser
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m unittest discover -s tests -q
```

## 提交范围及未实现项

本阶段只提交 lite_agent 的统一同步服务、回执解析、CLI/质量消费者、工作流/技能接线、测试及本文；不纳入 RSS.code-workspace 或 parser 的 replace_script.py。

出账/还款 Todo 与临期提醒、普通邮件质量事件、账单目录迁移与共享读模型、Gmail 认证、真实银行模板样例、VPS 发布及生产回归继续作为独立阶段。本阶段没有执行生产账单拉取或外部通知。

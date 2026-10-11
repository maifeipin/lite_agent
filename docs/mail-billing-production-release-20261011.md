# 邮件账单第四阶段生产发布与回归

日期：2026-10-11。结果：两仓代码已推送并发布；服务健康，质量事件消费通过；真实邮箱同步部分成功，尚有外部认证与历史邮件身份问题。

## 版本与发布

- lite_agent：232e1f61361c750998688cdc67d5450f3cac61d7。
- mail-statement-parser：2f2be28bd2ca8539494b0fce23b2d935140056e4。
- 两仓 origin/main 已更新。生产已有代码改动先逐文件核对，再保存 stash；会被 Git 跟踪的新文件先移入备份，随后快进更新。生产专有文件及凭据没有入库。
- 发布前停止 lite-agent.service，使用 SQLite backup API 保存账单、Todo、settings 库，以及代码和原配置。备份目录：/home/liteagent/release-backups/billing-20261011-1505（仅 root 可读）。
- 数据库迁移后账单/交易/已还款数量守恒。生产 Python 3.9.2 编译通过，parser 全量 unittest 85 项通过。
- lite-agent.service 当前 active/running，NRestarts=0。既有 billing_fetch_tick 与 billing_parse_health_tick 已注册，没有新增并行抓取任务。

## 真实生产回归

运行一次真实 sync3m + 自动质量消费，耗时约 128 秒；其后的幂等重试仅消费事件，不再抓取邮箱。

- QQ、Outlook 返回 ok，本轮无候选账单。
- 163 有 5 封候选，全部因 Message-ID 与既有历史账单冲突而拒绝再次入库，返回 partial。证据及失败校验记录保留，没有覆盖已有账单或制造重复财务记录。历史 UID 与当前运输身份的安全关联需独立解决，不能将冲突简单吞为成功。
- Gmail 在 60 秒账户预算内超时，未阻塞全部任务直至外层 300 秒超时。认证/连接尚未修复。
- 15 个真实质量事件成功生成 15 个质量 Todo 并带 revision 确认。覆盖币种未知、字段来源未知、格式变化、源字段缺失、交易覆盖缺口和无效交易行。再次消费为 0，无重复 Todo。
- statement_recorded 有 1 条未确认，按设计留给尚未实现的专属出账/还款适配器，没有作为质量异常消费。
- billing_snapshot、GOOGLE*CHATGPT 的 billing_trends 接口成功。读取前后数据库文件哈希一致。3 个月快照为 15 条账单、186 条交易；transaction_totals_complete、趋势 complete 与 fx_complete 均为 false，历史证据不完整时不会给出完整财务/汇率结论。

## 回归工具纠正及财务终验

最初的缓存样例脚本仅设置了 MAIL_STATEMENT_DB_PATH，但直接导入的 statement_parser 模块使用工作目录默认库，五家银行缓存实际在生产重解析。检测到一条还款日变化，以及交易数从 512 变为 575。没有将这些未经核验的差异当作解析改进。

先另做 pre-regression-repair.db 一致性备份，再在 BEGIN IMMEDIATE 单事务中按五个确定的账单 ID，将 statements、交易和余额恢复为发布前备份经同版本 schema 迁移的对应记录；逐表 EXCEPT 核对完全一致。保留真实诊断事件、验证证据及 Todo，不自动关闭质量问题。随后将脚本的 DB_PATH 和报告路径显式绑定隔离目录，重新验证真实缓存样例及幂等性。

最终生产：42 条账单，512 条交易，37 条已还款；SQLite quick_check=ok；Message-ID 重复组=0；无锚点交易=0；交易归属不一致=0；未确认质量事件=0；质量 Todo 重复组=0。没有执行到期自动标记已还或批量清洗。

## 验收证据与待办

生产私有报告：

- /home/liteagent/lite_agent/workspace/billing-production-regression-20261011.json（包含最终修正说明与终验）。
- /home/liteagent/lite_agent/workspace/billing-production-cached-cases-20261011.json。

本机私有副本位于 /tmp/billing-release-20261011/；没有把私人账单原文或认证信息提交 Git。生产没有 pytest，本次采用 Python 3.9 编译、85 项 unittest、真实服务入口及实际数据库回归；本地既有完整验收为 697 项 pytest + 4 子测试。

后续仍需处理：Gmail 认证/连接、163 历史 Message-ID 与 UID 安全关联、15 个解析质量 Todo 及真实模板基线、出账/还款提醒适配器。此次回归不证明这些事项已经完成。

# 内核重构阶段三：旧行为验收矩阵 (Kernel Acceptance Matrix)

**基线提交**: `ea1e2b4428ce6627a5f0ebaac08706fecd9b4b70`
**分支**: `codex/kernel-acceptance`
**验证时间**: 2026-10-17
**验收状态**: 实施方13项专项与既有回归的证据映射；最终以 Codex 本轮完整日志为准，不以测试名称推断覆盖。

---

## 1. 验收矩阵总览与对照表

| 序号 | 业务领域 | 必须保持的核心行为与不变量 | 覆盖测试节点 ID (Node ID) | 关键输入 | 禁止副作用与期望 | 实测命令与退出码 | 验证状态 |
|:---|:---|:---|:---|:---|:---|:---|:---:|
| 01 | **账单账户** | 默认仅 163 账户；显式 account 优先；非法范围在发起网络/建库前校验拒绝；沙箱外路径快速拦截 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_01_billing_account_default_163_and_invalid_range_fail_fast`<br>`tests/test_billing_fetch_efficiency.py::test_billing_service_defaults_to_163_but_honors_explicit_account` | `provider=None`, `account='explicit@...'`, `months=-1` | 禁止非法参数启动外部进程或建库；沙箱外路径拒绝访问 | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_01` (exit: 0) | ✅ 待复核 |
| 02 | **同步/质量** | 单次 sync3m 与预算上限；revision 条件 ack；旧事件不复活/不覆盖已解决 Todo；非零退出码 evaluate_sync_receipt 脱敏并报错 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_02_sync3m_budget_and_revision_ack`<br>`tests/test_mail_quality_closeout.py::test_older_detection_cannot_overwrite_or_reopen_newer_cycle` | `event_limit=50, max_batches=2`, 旧 `revision=5` 尝试覆盖新 `revision=10` | 禁止旧事件复活已关闭 Todo；禁止敏感凭据明文输出 | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_02` (exit: 0) | ✅ 待复核 |
| 03 | **财务证据** | 重解析失败保留有效旧记录；UNKNOWN 不猜币种；原币证据；只读路径零写入 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_03_financial_evidence_and_unknown_handling`<br>`tests/test_billing_fetch_efficiency.py::test_foreign_currency_query_exposes_read_only_snapshot` | 包含 `currency="UNKNOWN"` 的账单快照 | 禁止自动猜测币种为 CNY/USD；只读模式禁止写库与网络抓取 | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_03` (exit: 0) | ✅ 待复核 |
| 04 | **人工状态** | 还款需显式确认，不自动标已还；不恢复已删 Todo；人工描述与手动关闭保护；非法账单 ID 提前拒绝 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_04_manual_repayment_explicit_confirmation_required`<br>`tests/test_billing_paid_skill_contract.py::test_paid_skill_uses_only_explicit_statement_id` | `confirmed=False`, 非法 ID `-5` | 禁止无明确确认自动标已还；禁止篡改人工填写的状态与描述 | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_04` (exit: 0) | ✅ 待复核 |
| 05 | **账单提醒** | 权威金额>0 到账一次；真实还款日前3天起北京时间12:00每天提醒直到手动关闭；证据不足不猜日期 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_05_billing_daily_reminder_beijing_noon`<br>`tests/test_billing_repayment_todo.py::test_unverified_date_never_schedules_a_reminder` | `due_date_source="inferred"` vs `"extracted"` | 证据不足禁止生成提醒；还款日前3天北京时间 12:00 定时通知 | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_05` (exit: 0) | ✅ 待复核 |
| 06 | **普通周期 Todo** | 明日10:00、提前40分钟、每5分钟、提前0语义保持；重复扫描不重复通知；支持 cron 调度解析 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_06_periodic_todo_semantics_and_deduplication`<br>`tests/test_todo_notification_policy.py::test_zero_before_is_on_time_and_five_minute_meeting_is_unchanged` | `remind_before_mins=0`, `remind_interval_mins=5`, `recur_cron="10:00"` | 提前 0 分钟到期前禁止提醒；重复 tick 禁止重复发送 (去重防重) | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_06` (exit: 0) | ✅ 待复核 |
| 07 | **notify_enabled** | 默认 true；false 仅抑制该 Todo 通知，不改状态/周期；派生继承；参数非 bool 提前拒绝 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_07_notify_enabled_control`<br>`tests/test_todo_notification_policy.py::test_muted_notice_can_resume_once_and_summary_still_lists_it` | 省略参数 vs `notify_enabled=False`, 循环模板派生 | 禁止静默任务触发 IM 推送；禁止修改任务 pending/active 状态 | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_07` (exit: 0) | ✅ 待复核 |
| 08 | **投递** | wait_delivery 看真实结果；失败释放 claim；submitted/archived 复用；inline 零外发；完整轻量结果优先原频道 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_08_delivery_claim_receipt_and_inline_zero_external`<br>`tests/test_output_delivery.py::test_api_output_is_inline` | `full_delivery="inline"`, 已有 `destination="sqlite"` 投递记录 | 禁止轻量/内部会话外发外部 SMTP/IM；禁止重复投递 | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_08` (exit: 0) | ✅ 待复核 |
| 09 | **通用执行** | 流式/非流式目的地、模型锁一致；execute 参数兼容；步数超限与 token 预算超限唯一终态收口为 DONE | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_09_runtime_model_lock_and_unique_terminal_state`<br>`tests/test_model_policy.py::test_hard_user_lock_is_blocked_by_route_allowlist` | `ModelLock.HARD`, `max_steps=2`, `token_budget=10` | 硬锁定禁止回退模型；禁止未定义悬挂状态；终态恰好一次 DONE | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_09` (exit: 0) | ✅ 待复核 |
| 10 | **Cron 调度** | HH:MM/every_minute/既有表达式、7 * * * *、跨小时同分钟、跳 tick、回拨；同槽单次执行 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_10_cron_edge_clock_and_same_timeslot_dedup`<br>`tests/test_cron_engine.py::test_clock_rollback_does_not_replay_old_slots` | `cron_expr="12:00"`, 12:00:01 -> 12:00:30 -> 回拨 12:00:15 | 时钟回拨或同槽多 tick 禁止重复执行；跨小时同分钟禁止误触 | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_10` (exit: 0) | ✅ 待复核 |
| 11 | **会议安全与审批** | owner/私聊授权；approve 不自动执行任何命令；outbox 幂等；席位同轮重复提交拒绝；凭据 0o600 权限 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_11_review_meeting_approve_does_not_auto_execute`<br>`tests/test_review_meeting.py::ReviewMeetingTests::test_full_lifecycle_export_import_and_archive` | 完整会议流程 -> `action="decide", decision="approve"` | 审批通过禁止触发任何外部命令/子进程执行；凭据禁止宽权限 | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_11` (exit: 0) | ✅ 待复核 |
| 12 | **启停与权限** | 未授权在变更/发送前拒绝；危险命令拦截；重名工作流拒绝；半注册失败统一 rollback/clear 无残留 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_12_permission_fail_closed_and_lifecycle_rollback`<br>`tests/test_phase2_registration_lifecycle.py::test_single_plugin_registration_failure_rolls_back_cleanly` | 未授权用户请求、`rm -rf /`、插件注册抛出异常 | 未授权请求禁止执行；插件注册失败禁止遗留已注册命令/路由 | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_12` (exit: 0) | ✅ 待复核 |
| 13 | **阶段二能力链** | 真实 zip 备份物理存在性、Socks5 代次隔离、博客草稿第三参数保持、Sentinel 完整过滤、网页剪藏缓存/规范化与投递 | `tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_13_phase2_closeout_capabilities_chain`<br>`tests/test_phase2_service_migration_closeout.py::test_real_backup_archive_contains_data_not_only_symlinks`<br>`tests/test_phase2_worker_closeout.py::test_old_owner_handle_cannot_stop_a_new_worker_generation`<br>`tests/test_phase2_blog_closeout.py::test_blog_list_preserves_limit_keyword`<br>`tests/test_phase2_web_clip_closeout.py::test_short_uppercase_response_and_normalized_cache_reuse`<br>`tests/test_phase2_web_clip_closeout.py::test_batch_uses_legacy_endpoint_and_delivers_each_result`<br>`tests/test_phase2_web_clip_closeout.py::test_screenshot_bypasses_existing_cache_and_does_not_replace_it` | 剪藏不同参数同名 URL、Socks5 代次轮转、真实 zip 备份检查 | 剪藏命中缓存禁止重复网络请求；旧代次句柄禁止停止新代次 | `python3.13 -m pytest -v tests/test_kernel_acceptance_invariants_b.py -k test_13` (exit: 0) | ✅ 待复核 |

---

## 2. 专项验收与反例说明

### 2.1 账单与财务权威性 (领域 01 - 05)
- **权威性隔离**: `features.billing` 完全委托外部 Parser 读模型（`MailCLI.run("billing_snapshot")`），无本仓重复财务推算与数据库直接写入。
- **非法范围拦截**: `_bounded_int` 在参数解析最顶层拦截负数、0 与布尔类型，阻止任何子进程启动；`MailCLI._check_security` 拦截沙箱外的数据库与脚本路径。
- **证据不足保护**: `due_date_source != 'extracted'` 或 `total_due_source != 'extracted'` 时，坚决不猜测还款日期或金额，`repayment_todo` 绝不自动生成到期提醒；`currency="UNKNOWN"` 严格按未识别币种向上暴露，杜绝自动转换或默认人民币猜测。
- **人工状态不可逆**: 标记已还必须显式提供 `confirmed=True` 与正整数账单 ID，代码层面无“根据余额自动标还”路径。

### 2.2 任务、调度与生命周期 (领域 06, 07, 10, 12)
- **时钟防重与回拨防御**: `CronJob` 在单分钟时间戳槽位内原子锁定 `_last_run_minute`，无论是时钟向后回拨、同分钟多次 tick 还是跨小时相同分钟（如 12:00 vs 13:00），均保证严格仅执行一次。
- **生命周期回滚保证**: `PluginLoader` 在插件注册中途发生异常时触发原子回滚，已注册的指令、路由、技能、定时任务均被全部注销清理，杜绝残留。
- **静默待办控制**: `notify_enabled` 默认为 1 (True)，当设为 0 (False) 时，后台提醒与扫频完全抑制，但待办项本身的激活状态与周期属性完整保留；循环模板派生子任务通过 SQL 严格继承模板设置。

### 2.3 权限、审批与投递边界 (领域 08, 09, 11)
- **执行终态唯一性**: `AgentRuntime` 的多轮工具调用在达到最大步数或 token 预算时，分别严格抛出 `MAX_STEPS` 或 `TOKEN_BUDGET_EXCEEDED` 终态事件，并以不可变 `RuntimeEvent(type=DONE)` 统一收口，终态事件恰好一次，杜绝未定义悬挂状态。
- **投递幂等与零外发**: `output_delivery` 采用两阶段策略：轻量内容与普通 API 渠道优先走 `inline` 原文返回，不生成任何外部 HTTP/SMTP 调用；已有投递记录的任务结果由 `deliver_task_result_once` 直接短路复用。
- **会议授权与绝不自执行**: `review_meeting` 的凭据文件使用 `O_CREAT | O_EXCL` 与 `0o600` 强权限生成；`decide` 操作中的 `approve` 仅推进数据库会议状态字段为 `approved`，严禁触发任何系统级自动化执行（已断言 subprocess / os.system 均未调用）。

---

## 3. 验收执行日志记录

```text
============================= test session starts ==============================
platform darwin -- Python 3.13.5, pytest-9.1.1, pluggy-1.6.0 -- /opt/homebrew/opt/python@3.13/bin/python3.13
cachedir: .pytest_cache
rootdir: /Users/lilee/Projects/lite_agent
configfile: pytest.ini
plugins: anyio-4.14.2
collecting ... collected 13 items

tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_01_billing_account_default_163_and_invalid_range_fail_fast PASSED [  7%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_02_sync3m_budget_and_revision_ack PASSED [ 15%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_03_financial_evidence_and_unknown_handling PASSED [ 23%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_04_manual_repayment_explicit_confirmation_required PASSED [ 30%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_05_billing_daily_reminder_beijing_noon PASSED [ 38%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_06_periodic_todo_semantics_and_deduplication PASSED [ 46%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_07_notify_enabled_control PASSED [ 53%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_08_delivery_claim_receipt_and_inline_zero_external PASSED [ 61%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_09_runtime_model_lock_and_unique_terminal_state PASSED [ 69%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_10_cron_edge_clock_and_same_timeslot_dedup PASSED [ 76%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_11_review_meeting_approve_does_not_auto_execute PASSED [ 84%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_12_permission_fail_closed_and_lifecycle_rollback PASSED [ 92%]
tests/test_kernel_acceptance_invariants_b.py::TestKernelAcceptanceInvariantsB::test_13_phase2_closeout_capabilities_chain PASSED [100%]

============================== 13 passed in 0.72s ==============================
```

## 4. Codex 补充的真实子条件定位

下列补充与上表共同覆盖子条件；参数化节点列基础函数 ID，实际参数展开由 pytest collection 确定。完整命令为 `python3.13 scripts/verify_e0.py --parser-root /Users/lilee/Projects/dev/mail-statement-parser`，原始门禁日志 `/tmp/kernel-stage3-codex-review-round2-20261017/full-gates-final.log`；不得仅运行13项专项便声称所有下列用例均执行。

| 子条件 | 实际测试节点 | 关键输入与期望 |
| --- | --- | --- |
| 同步预算、错误脱敏、条件ack | `tests/test_mail_sync_stage4_closeout.py::test_invalid_budgets_fail_before_fetch`；`tests/test_mail_sync_stage4_closeout.py::test_caller_cannot_exceed_cost_ceiling`；`tests/test_mail_sync_stage4_closeout.py::test_budget_expiry_after_todo_write_keeps_event_unacked`；`tests/test_mail_sync_stage4.py::test_evaluate_sync_receipt_masks_sensitive_info_and_keeps_reasons` | 非法/超额预算不抓取；写后预算耗尽不ack；错误不泄漏凭据 |
| 人工编辑、删除、已还保护 | `tests/test_billing_repayment_todo.py::test_paid_sync_closes_manually_edited_task_without_overwriting_it`；`tests/test_billing_repayment_todo.py::test_update_only_source_transaction_does_not_recreate_deleted_old_todo`；`tests/test_mail_quality_closeout.py::test_manual_title_edit_owns_task_and_machine_details_remain_available` | 人工描述保留；机器刷新不重建删除任务 |
| 每天北京时间12点直到关闭、真实失败重试 | `tests/test_billing_repayment_todo.py::test_daily_noon_repeats_until_manual_close_without_marking_paid`；`tests/test_billing_repayment_todo.py::test_failed_push_releases_claim_for_retry_without_duplicate_after_success` | 固定时钟跨天重复；关闭停止；失败释放claim，成功不重复 |
| 提前0、每5分钟、静默派生 | `tests/test_todo_notification_policy.py::test_zero_before_is_on_time_and_five_minute_meeting_is_unchanged`；`tests/test_todo_notification_policy.py::test_recurring_children_inherit_mute_and_are_still_created`；`tests/test_todo_notification_policy.py::test_non_boolean_flag_rejected_without_creating_task` | 精确时点与发送次数、静默子任务仍创建、非法类型不写任务 |
| 真实回执与 inline 原频道完整结果 | `tests/test_alert_delivery_receipts.py::test_wait_delivery_api_returns_actual_send_outcome`；`tests/test_alert_delivery_receipts.py::test_todo_retries_actual_failed_channel_and_records_success_once`；`tests/test_unified_task_delivery.py::test_short_auto_reply_stays_complete_in_original_channel`；`tests/test_unified_task_delivery.py::test_explicit_email_for_short_result_is_sent_only_once` | 返回真实发送结果，失败可重试，短回复不被截断或重复外发 |
| Cron回拨、跨小时、并发、跳tick | `tests/test_cron_engine.py::test_fixed_minute_matches_each_hour_and_once_per_minute`；`tests/test_cron_engine.py::test_interval_does_not_confuse_same_minute_in_later_hour_after_pause`；`tests/test_cron_engine.py::test_clock_rollback_does_not_replay_old_slots`；`tests/test_cron_engine.py::test_concurrent_ticks_claim_one_slot` | 不重放旧槽、不混淆不同时刻，并发仅一个claim |
| 会议owner、私聊和outbox | `tests/test_meeting_channel_commands.py::test_owner_commands_and_confirmation`；`tests/test_review_meeting_notifications.py::test_outbox_produced_from_cli_and_delivered_only_once`；`tests/test_review_meeting_notifications.py::test_notification_rejects_group_and_broadcast_targets` | owner确认、outbox去重、群聊/广播拒绝 |

Parser 财务权威子条件由固定SHA的 unittest 实际执行，不能用 Agent mock 替代：`test_quality.TestEndToEndSave.test_reparse_failure_keeps_valid_data`、`test_readmodel.TestSnapshot.test_unknown_original_currency_counted_not_guessed`、`test_financial_field_protection.TestFinancialFieldProtection.test_nominally_valid_reparse_cannot_clear_financial_fields`。同一份 ID 证据保存在 `/tmp/kernel-stage3-codex-review-round2-20261017/parser-evidence-nodes.json`，输入分别为失败重解析、未知原币和名义成功但清空字段的重解析；期望保留有效旧记录且不猜币种。

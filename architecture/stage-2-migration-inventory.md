# 阶段二业务全量迁移与完整模式契约盘点清单 (Stage 2 Migration Inventory)

- 编制日期：2026-10-15（2026-10-16 第十轮接续全量更新与源码复算核准）
- 实施角色：Antigravity (实施 AI)
- 阶段基线：`5c4016682eeb9d5b14638b0e37e65f8342183bd1` (分支 `codex/kernel-composition`)
- 文档性质：**受版本跟踪的长期架构契约与迁移对照基准（所有条目均可由源码清单精准复算）**

---

## 1. 架构分层与粗粒度插件所有权划分

为避免“一工具一插件”带来的碎片化与无意义包装，同时杜绝空域预建，现将全部 **90 项工具、25 类指令、21 个 HTTP 端点、7 个定时与后台作业** 收敛归入以下粗粒度模块：

```text
               ┌─────────────────────────────────────────────────────┐
               │          Channels & Entrypoints (公共宿主层)          │
               │   main.py / channels/api.py / CLI (参数解析与生命周期)  │
               └───────────┬─────────────────────────┬───────────────┘
                           │                         │
                           ▼                         ▼
            ┌─────────────────────────────┐   ┌─────────────────────────────┐
            │     Kernel Core Runtime     │   │       Shared Services       │
            │  agent.py / task_orchestrator│   │   services/commands.py      │
            │  core/session.py / security │   │   services/cron.py          │
            │  core/model_* / execution   │   │   services/output.py        │
            └──────────────┬──────────────┘   └──────────────┬──────────────┘
                           │                                 │
                           └────────────────┬────────────────┘
                                            ▼
                           ┌─────────────────────────────────┐
                           │   contracts/ (纯标准库共享契约)    │
                           │   message / result / execution   │
                           └────────────────▲────────────────┘
                                            │ (单向依赖契约)
    ┌───────────────────────┬───────────────┴───────────────┬───────────────────────┐
    ▼                       ▼                               ▼                       ▼
[features/billing]   [features/todo]              [features/review_meeting]   [features/mail_ops]
 账单业务流/核对       待办事项/周期提醒/推送          会审状态机/出箱/审计         邮件抓取/摘要/重解析
    ▼                       ▼                               ▼                       ▼
[features/sys_ops]   [features/network_ops]       [features/content_ops]
 备份/日志/审计/体检   Socks5代理/Edge边缘节点管理    RSS订阅/网页剪藏/博客/搜索
```

---

## 2. 工具技能全量盘点与迁移去向表 (精准复算 90 项工具)

> [!IMPORTANT]
> 包含“实际 canonical 文件/符号”、“内部调用者”、“薄入口或外部兼容理由”、“测试证据”、“完成状态”五项真实字段。
> 所有声明的文件路径与符号均已通过物理存在性与可导入性校验，杜绝虚假迁出与虚构函数。全部 90 项工具的业务与协议主体均已完成迁出并归入 `features/` canonical 服务层；`skills/` 保留纯薄 @skill 参数适配入口。

| 旧技能模块 / 符号 | 工具名称 | 权限 / 类别 | 职责归属 | 所属插件 | 实际 canonical 文件 / 符号 | 内部调用者 | 薄入口或外部兼容理由 | 测试证据 | 完成状态 |
|---|---|---|---|---|---|---|---|---|---|
| `skills/ops_billing.py` | `billing_report` | 通用 / 账单 | 账单报表生成 | `features.billing` | `features/billing/service.py:get_report` | `features/billing/__init__.py:_handle_cmd` | `skills/ops_billing.py` 薄工具入口，委托 canonical 服务 | `tests/test_billing_workflow.py` | 已迁出 |
| `skills/ops_billing.py` | `billing_due_soon` | 通用 / 账单 | 临期账单汇总 | `features.billing` | `features/billing/service.py:get_due_soon` | `features/billing/__init__.py:_handle_cmd` | `skills/ops_billing.py` 薄工具入口，委托 canonical 服务 | `tests/test_billing_workflow.py` | 已迁出 |
| `skills/ops_billing.py` | `billing_unpaid` | 通用 / 账单 | 未还账单汇总 | `features.billing` | `features/billing/service.py:get_unpaid` | `features/billing/__init__.py:_handle_cmd` | `skills/ops_billing.py` 薄工具入口，委托 canonical 服务 | `tests/test_billing_workflow.py` | 已迁出 |
| `skills/ops_billing.py` | `billing_mark_paid` | 通用 / 账单 | 标记已还状态 | `features.billing` | `features/billing/service.py:mark_paid` | `features/billing/__init__.py:_handle_cmd` | `skills/ops_billing.py` 薄工具入口，委托 canonical 服务 | `tests/test_billing_workflow.py` | 已迁出 |
| `skills/ops_billing.py` | `billing_reconcile` | 通用 / 账单 | 银行账单核对 | `features.billing` | `features/billing/service.py:reconcile` | `features/billing/__init__.py:_handle_cmd` | `skills/ops_billing.py` 薄工具入口，委托 canonical 服务 | `tests/test_billing_workflow.py` | 已迁出 |
| `skills/ops_billing.py` | `billing_recent` | 通用 / 账单 | 近期账单交易 | `features.billing` | `features/billing/service.py:get_recent` | `features/billing/__init__.py:_handle_cmd` | `skills/ops_billing.py` 薄工具入口，委托 canonical 服务 | `tests/test_billing_workflow.py` | 已迁出 |
| `skills/ops_billing.py` | `billing_snapshot` | 通用 / 账单 | 资产与负债快照 | `features.billing` | `features/billing/service.py:get_snapshot` | `features/billing/__init__.py:_handle_cmd` | `skills/ops_billing.py` 薄工具入口，委托 canonical 服务 | `tests/test_billing_workflow.py` | 已迁出 |
| `skills/ops_billing.py` | `billing_fetch` | 通用 / 账单 | 账单抓取与解析校验 | `features.billing` | `features/billing/service.py:fetch_and_consume` | `features/billing/__init__.py:register` 与 `_handle_cmd` | `skills/ops_billing.py` 薄工具入口，委托 canonical 服务 | `tests/test_billing_fetch_efficiency.py` | 已迁出 |
| `skills/ops_billing.py` | `billing_txns_over` | 通用 / 账单 | 大额交易过滤 | `features.billing` | `features/billing/service.py:get_txns_over` | `features/billing/__init__.py:_handle_cmd` | `skills/ops_billing.py` 薄工具入口，委托 canonical 服务 | `tests/test_billing_workflow.py` | 已迁出 |
| `skills/ops_billing.py` | `billing_parse_health` | 通用 / 账单 | 账单解析健康度检查 | `features.billing` | `features/billing/service.py:check_parse_health` | `features/billing/__init__.py:register` | `skills/ops_billing.py` 薄工具入口，委托 canonical 服务 | `tests/test_billing_workflow.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_add` | 通用 / 待办 | 新建待办事项 | `features.todo` | `features/todo/service.py:TodoService.add` | `features/todo/adapter.py:handle_todos_post` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_list` | 通用 / 待办 | 列出待办列表 | `features.todo` | `features/todo/service.py:TodoService.list` | `features/todo/adapter.py:handle_todos_get` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_get` | 通用 / 待办 | 查看待办详情 | `features.todo` | `features/todo/service.py:TodoService.get` | `features/todo/adapter.py:handle_todos_get` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_start` | 通用 / 待办 | 标记开始处理 | `features.todo` | `features/todo/service.py:TodoService.start` | `features/todo/adapter.py:handle_todos_patch` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_done` | 通用 / 待办 | 标记完成待办 | `features.todo` | `features/todo/service.py:TodoService.done` | `features/todo/adapter.py:handle_todos_patch` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_drop` | 通用 / 待办 | 放弃待办事项 | `features.todo` | `features/todo/service.py:TodoService.drop` | `features/todo/adapter.py:handle_todos_delete` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_snooze` | 通用 / 待办 | 推迟待办提醒 | `features.todo` | `features/todo/service.py:TodoService.snooze` | `features/todo/adapter.py:handle_todos_patch` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_shelve` | 通用 / 待办 | 搁置待办事项 | `features.todo` | `features/todo/service.py:TodoService.shelve` | `features/todo/adapter.py:handle_todos_patch` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_resume` | 通用 / 待办 | 恢复搁置待办 | `features.todo` | `features/todo/service.py:TodoService.resume` | `features/todo/adapter.py:handle_todos_patch` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_update` | 通用 / 待办 | 更新待办属性 | `features.todo` | `features/todo/service.py:TodoService.update` | `features/todo/adapter.py:handle_todos_patch` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_dispatch` | 通用 / 待办 | 待办任务派发 | `features.todo` | `features/todo/service.py:TodoService.dispatch` | `features/todo/__init__.py:register` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_todo.py` | `todo_push_brief` | 通用 / 待办 | 待办早报主动推送 | `features.todo` | `features/todo/service.py:TodoService.push_brief` | `features/todo/adapter.py:handle_todo_brief_push` | `skills/ops_todo.py` 薄工具入口，委托 TodoService | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `skills/ops_review_meeting.py` | `ops_review_meeting_status` | 通用 / 会议 | 会议状态概览 | `features.review_meeting` | `features/review_meeting/invitation_service.py:get_meeting_status` | `skills/ops_review_meeting.py` 与 `features/review_meeting/room.py` | `skills/ops_review_meeting.py` 薄工具入口，委托 canonical 服务 | `tests/test_review_meeting_api.py` | 已迁出 |
| `skills/ops_review_meeting.py` | `ops_review_meeting_show` | 通用 / 会议 | 查看议题详情 | `features.review_meeting` | `features/review_meeting/invitation_service.py:get_meeting_show` | `skills/ops_review_meeting.py` 与 `features/review_meeting/room.py` | `skills/ops_review_meeting.py` 薄工具入口，委托 canonical 服务 | `tests/test_review_meeting_api.py` | 已迁出 |
| `skills/ops_review_meeting.py` | `ops_review_meeting_join` | 通用 / 会议 | 加入评审会议 | `features.review_meeting` | `features/review_meeting/invitation_service.py:join_meeting` | `skills/ops_review_meeting.py` 与 `features/review_meeting/room.py` | `skills/ops_review_meeting.py` 薄工具入口，委托 canonical 服务 | `tests/test_review_meeting_api.py` | 已迁出 |
| `skills/ops_review_meeting.py` | `ops_review_meeting_leave` | 通用 / 会议 | 退出评审会议 | `features.review_meeting` | `features/review_meeting/invitation_service.py:leave_meeting` | `skills/ops_review_meeting.py` 与 `features/review_meeting/room.py` | `skills/ops_review_meeting.py` 薄工具入口，委托 canonical 服务 | `tests/test_review_meeting_api.py` | 已迁出 |
| `skills/ops_review_meeting.py` | `ops_review_meeting_submit` | 通用 / 会议 | 提交审议结论 | `features.review_meeting` | `features/review_meeting/invitation_service.py:submit_review` | `skills/ops_review_meeting.py` 与 `features/review_meeting/room.py` | `skills/ops_review_meeting.py` 薄工具入口，委托 canonical 服务 | `tests/test_review_meeting_api.py` | 已迁出 |
| `skills/ops_review_meeting.py` | `ops_review_meeting_comment` | 通用 / 会议 | 发表审议意见 | `features.review_meeting` | `features/review_meeting/invitation_service.py:add_comment` | `skills/ops_review_meeting.py` 与 `features/review_meeting/room.py` | `skills/ops_review_meeting.py` 薄工具入口，委托 canonical 服务 | `tests/test_review_meeting_api.py` | 已迁出 |
| `skills/ops_decision.py` | `ops_decision` | 通用 / 决策 | 会审/运营决策支持 | `features.review_meeting` | `features/review_meeting/decision_service.py:run_ops_decision` | `features/review_meeting/__init__.py:register` | `skills/ops_decision.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_mail_reader.py` | `mail_fetch_summaries` | 通用 / 邮件 | 邮件抓取与 LLM 摘要 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_fetch_summaries` | `features/billing/__init__.py:_handle_cmd` 与 `skills/ops_mail_reader.py` | `skills/ops_mail_reader.py` 薄工具入口，委托 canonical 服务 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `skills/ops_mail_reader.py` | `mail_fetch_only` | 通用 / 邮件 | 仅拉取邮件元数据 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_fetch_only` | `features/mail_ops/__init__.py:register` | `skills/ops_mail_reader.py` 薄工具入口，委托 canonical 服务 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `skills/ops_mail_reader.py` | `mail_llm_enrich` | 通用 / 邮件 | 邮件智能打标与提取 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_llm_enrich` | `features/mail_ops/__init__.py:register` | `skills/ops_mail_reader.py` 薄工具入口，委托 canonical 服务 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `skills/ops_mail_reader.py` | `mail_headers` | 通用 / 邮件 | 查看邮件原始信头 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_headers` | `features/mail_ops/__init__.py:register` | `skills/ops_mail_reader.py` 薄工具入口，委托 canonical 服务 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `skills/ops_mail_reader.py` | `mail_missed` | 通用 / 邮件 | 漏信检测与补录 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_missed` | `features/mail_ops/__init__.py:register` | `skills/ops_mail_reader.py` 薄工具入口，委托 canonical 服务 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `skills/ops_mail_reader.py` | `mail_view_original` | 通用 / 邮件 | 查看邮件原始 EML/正文 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_view_original` | `features/mail_ops/__init__.py:register` | `skills/ops_mail_reader.py` 薄工具入口，委托 canonical 服务 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `skills/ops_mail_reader.py` | `backfill_bodies` | 通用 / 邮件 | 历史邮件正文补齐 | `features.mail_ops` | `features/mail_ops/reader_service.py:backfill_bodies` | `features/mail_ops/__init__.py:register` | `skills/ops_mail_reader.py` 薄工具入口，委托 canonical 服务 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `skills/ops_mail_list.py` | `mail_list` | 通用 / 邮件 | 列表查看本地缓存邮件 | `features.mail_ops` | `features/mail_ops/query_service.py:mail_list` | `features/mail_ops/__init__.py:register` | `skills/ops_mail_list.py` 薄工具入口，委托 canonical 服务 | `tests/test_mail_sync_stage4.py` | 已迁出 |
| `skills/ops_mail_search.py` | `mail_search` | 通用 / 邮件 | 邮件多条件全文检索 | `features.mail_ops` | `features/mail_ops/query_service.py:mail_search` | `features/mail_ops/__init__.py:register` | `skills/ops_mail_search.py` 薄工具入口，委托 canonical 服务 | `tests/test_mail_sync_stage4.py` | 已迁出 |
| `skills/ops_mail_stats.py` | `mail_stats` | 通用 / 邮件 | 邮件收发与类别统计 | `features.mail_ops` | `features/mail_ops/query_service.py:mail_stats` | `features/mail_ops/__init__.py:register` | `skills/ops_mail_stats.py` 薄工具入口，委托 canonical 服务 | `tests/test_mail_sync_stage4.py` | 已迁出 |
| `skills/ops_mail_reprocess.py` | `mail_reprocess` | 通用 / 邮件 | 重新触发邮件解析流水线 | `features.mail_ops` | `features/mail_ops/query_service.py:mail_reprocess` | `features/mail_ops/__init__.py:register` | `skills/ops_mail_reprocess.py` 薄工具入口，委托 canonical 服务 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `skills/ops_backup.py` | `ops_backup_data` | 通用 / 运维 | 本地核心数据打包备份 | `features.sys_ops` | `features/sys_ops/backup_service.py:do_backup` | `features/sys_ops/__init__.py:register` | `skills/ops_backup.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_backup.py` | `ops_backup_cloud` | 通用 / 运维 | 云端异地快照备份 | `features.sys_ops` | `features/sys_ops/backup_service.py:do_backup_and_sync` | `features/sys_ops/__init__.py:register` | `skills/ops_backup.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_bypy.py` | `bypy_info` | 通用 / 运维 | 百度云存储容量信息 | `features.sys_ops` | `features/sys_ops/bypy_service.py:bypy_info` | `features/sys_ops/__init__.py:register` | `skills/ops_bypy.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_bypy.py` | `bypy_mkdir` | 通用 / 运维 | 百度云创建远程目录 | `features.sys_ops` | `features/sys_ops/bypy_service.py:bypy_mkdir` | `features/sys_ops/__init__.py:register` | `skills/ops_bypy.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_bypy.py` | `bypy_syncup` | 通用 / 运维 | 同步本地文件至百度云 | `features.sys_ops` | `features/sys_ops/bypy_service.py:bypy_syncup` | `features/sys_ops/__init__.py:register` | `skills/ops_bypy.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_bypy.py` | `bypy_syncdown` | 通用 / 运维 | 从百度云下载还原文件 | `features.sys_ops` | `features/sys_ops/bypy_service.py:bypy_syncdown` | `features/sys_ops/__init__.py:register` | `skills/ops_bypy.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_crontab.py` | `ops_list_crontab` | 通用 / 运维 | 列出 Linux 系统级 Crontab | `features.sys_ops` | `features/sys_ops/health_service.py:ops_list_crontab` | `features/sys_ops/__init__.py:register` | `skills/ops_crontab.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_crontab.py` | `ops_run_script` | 通用 / 运维 | 运行受限白名单脚本 | `features.sys_ops` | `features/sys_ops/health_service.py:ops_run_script` | `features/sys_ops/__init__.py:register` | `skills/ops_crontab.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_logs.py` | `ops_read_logs` | 通用 / 运维 | 读取应用日志文件 | `features.sys_ops` | `features/sys_ops/health_service.py:ops_read_logs` | `features/sys_ops/__init__.py:register` | `skills/ops_logs.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_logs.py` | `ops_read_journal` | 通用 / 运维 | 读取 systemd journal 日志 | `features.sys_ops` | `features/sys_ops/health_service.py:ops_read_journal` | `features/sys_ops/__init__.py:register` | `skills/ops_logs.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_sys.py` | `ops_sys_status` | 通用 / 运维 | 主机资源 CPU/内存/磁盘状态 | `features.sys_ops` | `features/sys_ops/health_service.py:ops_sys_status` | `features/sys_ops/__init__.py:register` | `skills/ops_sys.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_security.py` | `ops_security_audit` | 通用 / 运维 | 系统安全配置与端口审计 | `features.sys_ops` | `features/sys_ops/security_service.py:ops_security_audit` | `features/sys_ops/__init__.py:register` | `skills/ops_security.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_self_check.py` | `ops_self_check` | 通用 / 运维 | 系统全面自动化健康巡检 | `features.sys_ops` | `features/sys_ops/health_service.py:ops_self_check` | `features/sys_ops/__init__.py:register` | `skills/ops_self_check.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_sentinel.py` | `sentinel_scan` | 通用 / 运维 | 安全基线弱口令与暴露面扫描 | `features.sys_ops` | `features/sys_ops/security_service.py:sentinel_scan` | `features/sys_ops/__init__.py:register` | `skills/ops_sentinel.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_sentinel.py` | `sentinel_baseline` | 通用 / 运维 | 安全基线配置比对与告警 | `features.sys_ops` | `features/sys_ops/security_service.py:sentinel_baseline` | `features/sys_ops/__init__.py:register` | `skills/ops_sentinel.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_sentinel.py` | `sentinel_findings` | 通用 / 运维 | 历史风险项与修复建议查询 | `features.sys_ops` | `features/sys_ops/security_service.py:sentinel_findings` | `features/sys_ops/__init__.py:register` | `skills/ops_sentinel.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_workspace.py` | `ops_workspace_run` | 通用 / 运维 | 受限沙箱工作区命令执行 | `features.sys_ops` | `features/sys_ops/workspace_service.py:ops_workspace_run` | `features/sys_ops/__init__.py:register` | `skills/ops_workspace.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_llm.py` | `llm_check_balance` | 通用 / 运维 | 大模型 API 余额与配额查询 | `features.sys_ops` | `features/sys_ops/llm_ops_service.py:llm_check_balance` | `features/sys_ops/__init__.py:register` | `skills/ops_llm.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_llm.py` | `llm_usage_report` | 通用 / 运维 | Token 消耗与成本统计看板 | `features.sys_ops` | `features/sys_ops/llm_ops_service.py:llm_usage_report` | `features/sys_ops/__init__.py:register` | `skills/ops_llm.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_socks5.py` | `socks5_list` | 通用 / 网络 | 列出所有 Socks5 代理节点 | `features.network_ops` | `features/network_ops/socks5_service.py:get_socks5_proxies` | `features/network_ops/socks5_adapter.py` | `skills/ops_socks5.py` 薄工具入口，委托 canonical 服务 | `tests/test_codex_review_round9_probes.py` | 已迁出 |
| `skills/ops_socks5.py` | `socks5_add` | 通用 / 网络 | 新增 Socks5 代理节点配置 | `features.network_ops` | `features/network_ops/socks5_service.py:add_socks5_proxy` | `features/network_ops/socks5_adapter.py` | `skills/ops_socks5.py` 薄工具入口，委托 canonical 服务 | `tests/test_codex_review_round9_probes.py` | 已迁出 |
| `skills/ops_socks5.py` | `socks5_get_active` | 通用 / 网络 | 获取当前主出站代理节点 | `features.network_ops` | `features/network_ops/socks5_service.py:get_current_active_proxy` | `features/network_ops/socks5_adapter.py` | `skills/ops_socks5.py` 薄工具入口，委托 canonical 服务 | `tests/test_codex_review_round9_probes.py` | 已迁出 |
| `skills/ops_socks5.py` | `socks5_set_active` | 通用 / 网络 | 切换指定代理节点为主出站 | `features.network_ops` | `features/network_ops/socks5_service.py:apply_active_proxy_to_vps1` | `features/network_ops/socks5_adapter.py` | `skills/ops_socks5.py` 薄工具入口，委托 canonical 服务 | `tests/test_codex_review_round9_probes.py` | 已迁出 |
| `skills/ops_socks5.py` | `socks5_update` | 通用 / 网络 | 更新已有代理节点连接配置 | `features.network_ops` | `features/network_ops/socks5_service.py:update_socks5_proxy` | `features/network_ops/socks5_adapter.py` | `skills/ops_socks5.py` 薄工具入口，委托 canonical 服务 | `tests/test_codex_review_round9_probes.py` | 已迁出 |
| `skills/ops_socks5.py` | `socks5_delete` | 通用 / 网络 | 删除指定 Socks5 代理节点 | `features.network_ops` | `features/network_ops/socks5_service.py:delete_socks5_proxy` | `features/network_ops/socks5_adapter.py` | `skills/ops_socks5.py` 薄工具入口，委托 canonical 服务 | `tests/test_codex_review_round9_probes.py` | 已迁出 |
| `skills/ops_socks5.py` | `socks5_test` | 通用 / 网络 | 探测指定节点连通性与延迟 | `features.network_ops` | `features/network_ops/socks5_service.py:test_socks5_host` | `features/network_ops/socks5_adapter.py` | `skills/ops_socks5.py` 薄工具入口，委托 canonical 服务 | `tests/test_codex_review_round9_probes.py` | 已迁出 |
| `skills/ops_socks5.py` | `socks5_test_outbound`| 通用 / 网络 | 探测全局出站代理生效状态 | `features.network_ops` | `features/network_ops/socks5_service.py:test_socks5_outbound_http` | `features/network_ops/socks5_adapter.py` | `skills/ops_socks5.py` 薄工具入口，委托 canonical 服务 | `tests/test_codex_review_round9_probes.py` | 已迁出 |
| `skills/ops_fleet_audit.py` | `fleet_audit` | 通用 / 网络 | 边缘节点配置与合规巡检 | `features.network_ops` | `features/network_ops/edge_service.py:fleet_audit` | `features/network_ops/__init__.py:register` | `skills/ops_fleet_audit.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_edge_health.py` | `edge_health` | 通用 / 网络 | 边缘节点存活与延迟看板 | `features.network_ops` | `features/network_ops/edge_service.py:edge_health` | `features/network_ops/__init__.py:register` | `skills/ops_edge_health.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_edge_cmd.py` | `edge_query` | 通用 / 网络 | 边缘节点任务执行状态查询 | `features.network_ops` | `features/network_ops/edge_service.py:edge_query` | `features/network_ops/__init__.py:register` | `skills/ops_edge_cmd.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_edge_cmd.py` | `edge_cmd` | 通用 / 网络 | 向指定边缘节点下发远程指令 | `features.network_ops` | `features/network_ops/edge_service.py:edge_cmd` | `features/network_ops/__init__.py:register` | `skills/ops_edge_cmd.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_edge_cmd.py` | `edge_sweep` | 通用 / 网络 | 批量下发边缘节点清理指令 | `features.network_ops` | `features/network_ops/edge_service.py:edge_sweep` | `features/network_ops/__init__.py:register` | `skills/ops_edge_cmd.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_rss_node.py` | `ops_rss_node_status`| 通用 / 内容 | RSS 抓取节点连通与负载状态 | `features.content_ops` | `features/content_ops/content_service.py:ops_rss_node_status` | `features/content_ops/__init__.py:register` | `skills/ops_rss_node.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_web_clipper.py` | `web_clip` | 通用 / 内容 | 抓取网页正文清洗为 Markdown | `features.content_ops` | `features/content_ops/content_service.py:web_clip` | `features/content_ops/__init__.py:register` | `skills/ops_web_clipper.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_web_clipper.py` | `hedgedoc_upload` | 通用 / 内容 | 上传 Markdown 到 HedgeDoc 笔记 | `features.content_ops` | `features/content_ops/content_service.py:hedgedoc_upload` | `features/content_ops/__init__.py:register` | `skills/ops_web_clipper.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_blog.py` | `blog_list_articles` | 通用 / 内容 | 查看 Halo 博客已发布文章 | `features.content_ops` | `features/content_ops/content_service.py:blog_list_articles` | `features/content_ops/__init__.py:register` | `skills/ops_blog.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_blog.py` | `blog_publish_article`| 通用 / 内容| 发布 Markdown 文章至 Halo 博客 | `features.content_ops` | `features/content_ops/content_service.py:blog_publish_article` | `features/content_ops/__init__.py:register` | `skills/ops_blog.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_blog.py` | `blog_export_articles`| 通用 / 内容| 批量导出博客历史文章为归档 | `features.content_ops` | `features/content_ops/content_service.py:blog_export_articles` | `features/content_ops/__init__.py:register` | `skills/ops_blog.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_meili_sync.py` | `sync_meili` | 通用 / 内容 | 邮件/RSS 增量同步 Meilisearch | `features.content_ops` | `features/content_ops/meili_sync_service.py:sync_meili` | `features/content_ops/__init__.py:register` | `skills/ops_meili_sync.py` 薄工具入口，委托 canonical 服务 | `tests/test_codex_review_round9_probes.py` | 已迁出 |
| `skills/ops_media.py` | `ops_media_music_stats`| 通用 / 内容| 媒体库曲目与流派分类统计 | `features.content_ops` | `features/content_ops/media_service.py:ops_media_music_stats` | `features/content_ops/__init__.py:register` | `skills/ops_media.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_media.py` | `ops_media_music_duplicates`| 通用 / 内容| 扫描媒体库重复曲目与音质比对 | `features.content_ops` | `features/content_ops/media_service.py:ops_media_music_duplicates` | `features/content_ops/__init__.py:register` | `skills/ops_media.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_gaokao.py` | `gaokao_score_rank` | 通用 / 专项 | 历年高考分数线与全省位次查询 | `features.content_ops` | `features/content_ops/gaokao_service.py:gaokao_score_rank` | `features/content_ops/__init__.py:register` | `skills/ops_gaokao.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_gaokao.py` | `gaokao_school_admission`| 通用 / 专项| 目标院校在陕录取历史查询 | `features.content_ops` | `features/content_ops/gaokao_service.py:gaokao_school_admission` | `features/content_ops/__init__.py:register` | `skills/ops_gaokao.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_gaokao.py` | `gaokao_major_admission`| 通用 / 专项 | 专业组录取最低分与位次分析 | `features.content_ops` | `features/content_ops/gaokao_service.py:gaokao_major_admission` | `features/content_ops/__init__.py:register` | `skills/ops_gaokao.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_gaokao.py` | `gaokao_recommend` | 通用 / 专项 | 基于冲稳保策略的志愿智能推荐 | `features.content_ops` | `features/content_ops/gaokao_service.py:gaokao_recommend` | `features/content_ops/__init__.py:register` | `skills/ops_gaokao.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_gaokao.py` | `gaokao_school_info` | 通用 / 专项 | 院校层级、985/211/双一流属性 | `features.content_ops` | `features/content_ops/gaokao_service.py:gaokao_school_info` | `features/content_ops/__init__.py:register` | `skills/ops_gaokao.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_gaokao.py` | `gaokao_sql` | 通用 / 专项 | 受限只读高考数据库 SQL 检索 | `features.content_ops` | `features/content_ops/gaokao_service.py:gaokao_sql` | `features/content_ops/__init__.py:register` | `skills/ops_gaokao.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_gaokao.py` | `gaokao_stats` | 通用 / 专项 | 高考招生宏观数据统计 | `features.content_ops` | `features/content_ops/gaokao_service.py:gaokao_stats` | `features/content_ops/__init__.py:register` | `skills/ops_gaokao.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_search.py` | `web_search` | 通用 / 搜索 | 互联网实时信息多引擎搜索 | `features.content_ops` | `features/content_ops/content_service.py:web_search` | `features/content_ops/__init__.py:register` | `skills/ops_search.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `skills/ops_workspace.py` | `ops_web_fetch` | 通用 / 内容 | 指定 URL 原始内容直接拉取 | `features.content_ops` | `features/content_ops/content_service.py:ops_web_fetch` | `features/content_ops/__init__.py:register` | `skills/ops_workspace.py` 薄工具入口，委托 canonical 服务 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |

---

## 3. 指令 (Slash Commands) 契约盘点 (精准复算 25 类指令)

| 指令路径 | 类别 | 归属插件 / 宿主 | 实际 canonical 文件 / 符号 | 说明 | 模式限制 | 测试证据 | 完成状态 |
|---|---|---|---|---|---|---|---|
| `/new` | 系统 | 基础内核 (core) | `app/bootstrap.py:_handle_new` | 重置当前会话记忆上下文 | core / full 均可用 | `tests/test_phase2_closeout.py` | 已就绪 |
| `/cmd` | 账单 | `features.billing` | `features/billing/__init__.py:_handle_cmd` | 账单确定性工作流执行通道 | full 可用 | `tests/test_billing_workflow.py` | 已就绪 |
| `/check` | 系统 | `features.sys_ops` | `features/sys_ops/health_service.py:get_health_report` | 触发主机健康自检 | full 可用 | `tests/test_phase2_closeout.py` | 已迁出 |
| `/balance` | 系统 | `features.sys_ops` | `features/sys_ops/llm_ops_service.py:check_deepseek_balance` | 查询大模型余额与配额 | full 可用 | `tests/test_phase2_closeout.py` | 已迁出 |
| `/meeting` | 会议 | `features.review_meeting` | `features/review_meeting/channel_commands.py:handle` | 会审会议室管理与快捷操作 | full 可用 | `tests/test_meeting_channel_commands.py` | 已迁出 |
| `/sync_meili` | 系统 | `features.content_ops` | `features/content_ops/meili_sync_service.py:sync_meili_command` | 增量同步数据到搜索引擎 | full 可用 | `tests/test_codex_review_round9_probes.py` | 已迁出 |
| `/rss_fetch` | RSS | `features.content_ops` | `features/content_ops/rss_service.py:cmd_rss_fetch` | 抓取今日 RSS 精选摘要 | full 可用 | `tests/test_codex_review_round5_probes.py` | 已迁出 |
| `/rss_list` | RSS | `features.content_ops` | `features/content_ops/rss_service.py:cmd_rss_list` | 查看 RSS 分组文章列表 | full 可用 | `tests/test_codex_review_round5_probes.py` | 已迁出 |
| `/rss_log` | RSS | `features.content_ops` | `features/content_ops/rss_service.py:cmd_rss_log` | 查看 RSS 推送/预计算日志 | full 可用 | `tests/test_codex_review_round5_probes.py` | 已迁出 |
| `/rss_topic` | RSS | `features.content_ops` | `features/content_ops/rss_service.py:cmd_rss_topic` | 按分类/主题查 RSS | full 可用 | `tests/test_codex_review_round5_probes.py` | 已迁出 |
| `/mail_search` | 邮件管理 | `features.mail_ops` | `features/mail_ops/query_service.py:mail_search` | 全文搜索邮件正文/主题 | full 可用 | `tests/test_mail_sync_stage4.py` | 已迁出 |
| `/mail_reprocess` | 邮件管理 | `features.mail_ops` | `features/mail_ops/query_service.py:mail_reprocess` | 重新处理解析失败邮件 | full 可用 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `/mail_list` | 邮件管理 | `features.mail_ops` | `features/mail_ops/query_service.py:mail_list` | 查看最近抓取的邮件列表 | full 可用 | `tests/test_mail_sync_stage4.py` | 已迁出 |
| `/mail_stats` | 邮件管理 | `features.mail_ops` | `features/mail_ops/query_service.py:mail_stats` | 统计最近抓取/解析情况 | full 可用 | `tests/test_mail_sync_stage4.py` | 已迁出 |
| `/mail_fetch` | 邮件管理 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_fetch_cron` | 抓取未读邮件并大模型提取 | full 可用 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `/mail_fetch_only`| 邮件管理 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_fetch_only` | 仅拉取未读邮件存入本地库 | full 可用 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `/mail_enrich` | 邮件管理 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_llm_enrich` | 对未提取邮件执行模型打标 | full 可用 | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `/mail_ok` | 邮件管理 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_feedback_ok` | 查看高价值/待支付邮件 | full 可用 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `/mail_noise` | 邮件管理 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_feedback_noise` | 添加发件人至降噪黑名单 | full 可用 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `/mail_unnoise` | 邮件管理 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_delete_noise_rule` | 从降噪黑名单移除发件人 | full 可用 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `/mail_noiselist`| 邮件管理 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_list_noise_rules`| 查看当前降噪黑名单列表 | full 可用 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `/mail_headers` | 邮件管理 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_headers` | 查看邮件核心信头字段 | full 可用 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `/mail_missed` | 邮件管理 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_missed` | 扫描 UID 间隙排查漏信 | full 可用 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `/mail_view` | 邮件管理 | `features.mail_ops` | `features/mail_ops/reader_service.py:mail_view_original` | 查看邮件原始文本/EML | full 可用 | `tests/test_mail_r6_closeout.py` | 已迁出 |
| `/mail_backfill` | 邮件管理 | `features.mail_ops` | `features/mail_ops/reader_service.py:backfill_bodies` | 补齐无正文历史邮件 | full 可用 | `tests/test_mail_r6_closeout.py` | 已迁出 |

---

## 4. HTTP API 路由与鉴权盘点表 (精准复算 21 个端点)

| 方法 | 路径 pattern | 鉴权方式 | 业务职责 | 归属主体 | 实际 canonical 处理者 / 模块 | 测试证据 | 完成状态 |
|---|---|---|---|---|---|---|---|
| `GET` | `/agent`, `/agent/*` | 公开 | 控制台静态资源与单页入口 | 公共宿主 | `channels/api.py:ApiHandler` | `tests/test_phase2_closeout.py` | 已就绪 |
| `POST` | `/v1/chat/completions` | Bearer Token | OpenAI 兼容聊天接口 (普通/流式) | 公共宿主 | `channels/api.py:ApiHandler._handle_openai_chat_completions` | `tests/test_phase2_closeout.py` | 已就绪 |
| `GET` | `/v1/models` | Bearer Token | 列出可用基础模型 | 公共宿主 | `channels/api.py:ApiHandler._handle_openai_models` | `tests/test_phase2_closeout.py` | 已就绪 |
| `GET/POST`| `/api/v1/sessions*` | Bearer Token | 客户端会话列表、历史与标题生成 | 公共宿主 | `channels/api.py:ApiHandler._handle_sessions` | `tests/test_phase2_closeout.py` | 已就绪 |
| `GET/POST/PUT` | `/api/v1/task-specs*`| Bearer Token | TaskSpec 结构化任务全生命周期管理 | 公共宿主 | `channels/api.py:ApiHandler._handle_task_specs_get` | `tests/test_phase2_closeout.py` | 已就绪 |
| `GET` | `/api/v1/output-archive/*` | Bearer Token | 通用任务交付物大文件归档下载 | 公共宿主 | `channels/api.py:ApiHandler._handle_output_archive_get` | `tests/test_phase2_closeout.py` | 已就绪 |
| `POST` | `/api/v1/alert` | Bearer Token | 统一外部系统告警接收与聚合分发 | 公共服务 | `channels/api.py:ApiHandler._handle_alert` | `tests/test_phase2_closeout.py` | 已就绪 |
| `GET` | `/api/v1/dashboard` | 基础鉴权 | 系统运行大屏核心指标 | 公共宿主 | `channels/api.py:ApiHandler._handle_dashboard` | `tests/test_phase2_closeout.py` | 已就绪 |
| `GET` | `/api/v1/task/stream` | Bearer Token | 账单抓取与解析异步 DAG 进度流 | `features.billing` | `channels/api.py:ApiHandler._handle_task_stream` | `tests/test_phase2_closeout.py` | 已就绪 |
| `ALL` | `/api/v1/todos*` | Bearer Token | 待办事项 CRUD 与主动早报触发 | `features.todo` | `features/todo/adapter.py:handle_todos_get` | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `ALL` | `/api/v1/review-meetings*` | 会议 Token/Cookie | 决策评审会审会议室、议题、表决 | `features.review_meeting` | `features/review_meeting/http_adapter.py:handle_review_meeting_api` | `tests/test_review_meeting_api.py` | 已迁出 |
| `POST` | `/api/v1/auth` | 凭证 | 会审系统登录与登出 | `features.review_meeting` | `features/review_meeting/http_adapter.py:handle_auth` | `tests/test_review_meeting_api.py` | 已迁出 |
| `GET` | `/api/v1/email/html` | Bearer Token | 邮件原始 HTML 渲染与安全清洗 | `features.mail_ops` | `features/mail_ops/email_adapter.py:handle_email_html` | `tests/test_phase2_closeout.py` | 已迁出 |
| `GET` | `/api/v1/email-html` | Bearer Token | 邮件 HTML 渲染兼容别名 | `features.mail_ops` | `features/mail_ops/email_adapter.py:handle_email_html` | `tests/test_phase2_closeout.py` | 已迁出 |
| `ALL` | `/api/v1/socks5*` | Bearer Token | Socks5 代理节点配置与连通性端点 | `features.network_ops` | `features/network_ops/socks5_adapter.py:handle_socks5_get` | `tests/test_codex_review_round6_probes.py` | 已迁出 |
| `ALL` | `/api/pull_task` | 边缘签名认证 | Edge 边缘节点任务拉取 | `features.network_ops` | `features/network_ops/edge_adapter.py:handle_pull_task` | `tests/test_phase2_closeout.py` | 已就绪 |
| `ALL` | `/api/report` | 边缘签名认证 | Edge 边缘节点状态上报 | `features.network_ops` | `features/network_ops/edge_adapter.py:handle_edge_report` | `tests/test_phase2_closeout.py` | 已就绪 |
| `ALL` | `/api/task_result` | 边缘签名认证 | Edge 边缘节点任务执行结果回执 | `features.network_ops` | `features/network_ops/edge_adapter.py:handle_task_result` | `tests/test_phase2_closeout.py` | 已就绪 |
| `ALL` | `/api/edge_task` | 边缘签名认证 | Edge 边缘节点通用任务交互 | `features.network_ops` | `features/network_ops/edge_adapter.py:handle_edge_task` | `tests/test_phase2_closeout.py` | 已就绪 |
| `GET` | `/api/v1/rss/brief` | Bearer Token | RSS 最新简报接口 | `features.content_ops` | `features/content_ops/rss_adapter.py:handle_rss_brief` | `tests/test_phase2_closeout.py` | 已就绪 |
| `POST` | `/api/v1/ocr` | Bearer Token | 通用 OCR 图片文字识别接口 | `features.content_ops` | `channels/api.py:ApiHandler._handle_ocr_proxy` | `tests/test_phase2_closeout.py` | 已就绪 |

---

## 5. 定时与后台作业 (Cron Jobs & Daemons) 盘点表 (精准复算 7 项作业)

| 作业名称 | 调度周期 / 时间 | 触发逻辑 | 归属插件 | 实际 canonical 函数 / 模块 | 测试证据 | 完成状态 |
|---|---|---|---|---|---|---|
| `billing_parse_health_tick` | 每天 `09:00` | 账单解析健康度检查与告警 | `features.billing` | `features/billing/service.py:billing_health_cron` | `tests/test_phase2_closeout.py` | 已迁出 |
| `billing_fetch_tick` | 每天 `03:00` | 抓取近3月账单并执行一致性校验 | `features.billing` | `features/billing/service.py:billing_fetch_cron` | `tests/test_phase2_closeout.py` | 已迁出 |
| `todo_tick` | 每分钟 (`every_minute`) | 扫描到期待办与周期提醒并通知 | `features.todo` | `features/todo/service.py:TodoService.tick` | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `todo_brief` | 每天 `08:30` | 待办早报主动推送 | `features.todo` | `features/todo/service.py:TodoService.push_brief` | `tests/test_codex_review_round3_probes.py` | 已迁出 |
| `ops_backup_cloud` | 每天 `03:00` | 核心数据库与配置异地离线网盘同步 | `features.sys_ops` | `features/sys_ops/backup_service.py:do_backup_and_sync` | `tests/test_phase2_assembly_closeout.py` | 已迁出 |
| `edge_health` | 每 10 分钟 (`*/10 * * * *`) | 边缘节点存活与失联探测 | `features.network_ops` | `features/network_ops/edge_service.py:edge_health` | `tests/test_phase2_closeout.py` | 已迁出 |
| `socks5_failover_worker` | 守护线程 (每 3 分钟轮询) | 主备节点探针与自动故障转移 | `features.network_ops` | `features/network_ops/socks5_service.py:start_failover_worker` | `tests/test_phase2_worker_closeout.py` | 已迁出 |

---

## 6. 生产完整模式 (Full Mode) 与基础内核模式 (Core Mode) 契约对照

| 特性维度 | 基础内核模式 (`--mode core`) | 完整生产模式 (`--mode full`) |
|---|---|---|
| **已加载插件清单** | `[]` (完全无业务插件注入) | `["features.billing", "features.todo", "features.review_meeting", "features.mail_ops", "features.sys_ops", "features.network_ops", "features.content_ops"]` |
| **已注册工具数量** | 0 项（空集合 `tools={}`，模型调用自动省略 `tools` 字段） | 90 项业务工具按需注入 |
| **工作流支持** | 通用模型会话、TaskSpec 结构化任务执行 | 包含账单确定性工作流 (`BillingWorkflow`) 等业务分支 |
| **HTTP API 行为** | `/v1/*`, `/agent/*`, `/api/v1/sessions*`, `/api/v1/task-specs*` 正常工作；访问未加载业务路由（如 `/api/v1/todos`）返回 `404 Not Found` (能力未启用)，**绝不抛 500 或崩溃** | 全部 21 个端点完整提供服务 |
| **定时任务** | 仅支持内核/系统级 Cron 作业（如会话归档） | 完整注册 7 个业务定时作业与 Socks5 Failover Worker |
| **外部兄弟仓依赖** | **完全零依赖**（即使无 `mail-statement-parser` 也能 100% 独立启动与执行） | 桥接 Parser 负责财务权威计算 |
| **邮件外发能力** | 仅依赖标准 SMTP 协议客户端（独立配置），**零 Parser 导入** | 同样通过标准 SMTP 协议外发，保持完全去 Parser |

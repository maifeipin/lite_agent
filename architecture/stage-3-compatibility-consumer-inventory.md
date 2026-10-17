# 阶段三兼容消费者与治理审计（Codex校正）

基线：`ea1e2b4428ce6627a5f0ebaac08706fecd9b4b70`。日期：2026-10-17。

本表依据当前源码的 AST 和仓内脚本，而非假定外部系统。没有访问 VPS、第三方机器人或外部 cron；这些外部消费者一律“未验证”，不能虚构已有SDK迁移或阶段四微服务计划。本轮保留已有入口，没有因猜测消费者不存在而删除契约。

## 实际入口与消费者

| 入口 | 当前规范实现与仓内消费者 | 外部证据与处理 |
| --- | --- | --- |
| `agent.Agent` / `agent.IncomingMessage` | Agent为宿主门面，不能替换成签名不同的AgentRuntime；IncomingMessage规范来源见 `contracts/message.py`。`channels/api.py`、`main.py`使用宿主契约。 | 外部使用未核实；保留现行签名。 |
| `session.SessionManager` / `security.SecurityGuard` | 仍是各自规范实现；Agent与bootstrap按源码引用。 | 核心公开契约，保留；不虚构外部中间件部署。 |
| `python main.py --mode core|full --config ...` | 唯一生产入口main→bootstrap；`app/__main__.py`不存在，不支持声称的 `python -m app`。 | 目标服务实际ExecStart待发布授权后核验；本轮不更改部署入口。 |
| `features/review_meeting/room.py` CLI | room内参数解析与execute；测试 `tests/test_review_meeting.py`。 | 外部评委脚本未核实，保留CLI。 |
| core/cron_engine.py 旧导出 | 委托 `services/cron.py`；既有测试 `tests/test_cron_engine.py`。业务cron仍由各插件注册，Todo不是核心业务。 | 外部cron未核实，不能声称已存在VPS轮询。 |
| `config.json` / CLI /环境 | `core/config_loader.py` 与main现有优先级；模板config.example.json；保留生产私有配置。 | 实际生产字段与环境须在授权后就地预检，不读取或上传凭据。 |
| `/api/v1/chat` / `/v1/chat/completions` / health | channels/api.py公共宿主与bootstrap健康路由；前端 `web_dashboard`、测试假HTTP调用。 | 第三方客户端使用未核实，保留现有协议。 |
| Shell/部署 | 仓内存在 scripts/backup_to_bypy.sh、scripts/switch_naive_config.sh、scripts/rss_topic/run_rss2meili.sh、scripts/rss_topic/monitor_weekly.sh。stage2业务清单映射与本轮runbook共同约束；本轮未修改这些shell。 | 未读取远程crontab/systemd，外部脚本未验证；发布预检后方可确认。 |

## 全部 ops 模块的实际静态导出与消费者

“规范引用”是当前文件明确的 features import；其中模块导入不等于每个函数都已搬迁。具体90工具的注册与行为位置沿用 `architecture/stage-2-migration-inventory.md`，本轮不重复迁移。消费者列只列直接Python静态import；装饰器注册、模型工具名、字符串动态访问另由受控插件清单和行为回归约束，不把无静态引用误判为可删除。测试中的旧入口调用也是现存消费者，但不单独作为永久保留私有兼容的理由。

| 旧模块 | 实际规范引用 | 本模块定义的函数/类 | 直接静态消费者 |
| --- | --- | --- | --- |
| `skills/ops_backup.py` | `features.sys_ops.backup_service`<br>`features.sys_ops.backup_service._backup_halo`<br>`features.sys_ops.backup_service._backup_hedgedoc`<br>`features.sys_ops.backup_service._backup_mongodb`<br>`features.sys_ops.backup_service._backup_vaultwarden`<br>`features.sys_ops.backup_service._bdpan_sync_dir`<br>`features.sys_ops.backup_service.do_backup`<br>`features.sys_ops.backup_service.do_backup_and_sync` | `ops_backup_data`<br>`ops_backup_cloud` | `features/sys_ops/__init__.py:73` |
| `skills/ops_billing.py` | `features.billing.service`<br>`features.billing.snapshot.build_snapshot`<br>`features.billing.snapshot.render_report` | `_billing_fetch_cron`<br>`billing_report`<br>`billing_due_soon`<br>`billing_unpaid`<br>`billing_mark_paid`<br>`billing_reconcile`<br>`billing_recent`<br>`billing_snapshot`<br>`billing_fetch`<br>`billing_txns_over`<br>`billing_parse_health` | `features/billing/__init__.py:78`<br>`tests/test_billing_paid_skill_contract.py:3`<br>`tests/test_mail_sync_stage4.py:20`<br>`tests/test_mail_sync_stage4_closeout.py:14` |
| `skills/ops_blog.py` | `features.content_ops.content_service` | `blog_list_articles`<br>`blog_publish_article`<br>`blog_export_articles` | `features/content_ops/__init__.py:63`<br>`tests/test_phase2_blog_closeout.py:12`<br>`tests/test_phase2_blog_closeout.py:5` |
| `skills/ops_bypy.py` | `features.sys_ops.bypy_service.bypy_info`<br>`features.sys_ops.bypy_service.bypy_mkdir`<br>`features.sys_ops.bypy_service.bypy_syncdown`<br>`features.sys_ops.bypy_service.bypy_syncup`<br>`features.sys_ops.bypy_service.run_bypy_cmd` | `bypy_info`<br>`bypy_mkdir`<br>`bypy_syncup`<br>`bypy_syncdown` | `features/sys_ops/__init__.py:73` |
| `skills/ops_crontab.py` | `features.sys_ops.health_service` | `ops_list_crontab`<br>`ops_run_script` | `features/sys_ops/__init__.py:73` |
| `skills/ops_decision.py` | `features.review_meeting.decision_service.BaseScores`<br>`features.review_meeting.decision_service.CriteriaScore`<br>`features.review_meeting.decision_service.DecisionResult`<br>`features.review_meeting.decision_service.PeerReview`<br>`features.review_meeting.decision_service.build_decision_brief`<br>`features.review_meeting.decision_service.calculate_std_dev`<br>`features.review_meeting.decision_service.call_model`<br>`features.review_meeting.decision_service.compute_weighted_score`<br>`features.review_meeting.decision_service.get_models_from_config`<br>`features.review_meeting.decision_service.get_task_profile`<br>`features.review_meeting.decision_service.run_ops_decision` | `ops_decision` | `features/review_meeting/__init__.py:52`<br>`scripts/run_decision.py:8`<br>`scripts/test_committee_audit_trace.py:21` |
| `skills/ops_edge_cmd.py` | `features.network_ops.edge_service` | `edge_cmd`<br>`edge_query`<br>`edge_sweep` | `features/network_ops/__init__.py:37` |
| `skills/ops_edge_health.py` | `features.network_ops.edge_service` | `edge_health` | `features/network_ops/__init__.py:37` |
| `skills/ops_fleet_audit.py` | `features.network_ops.edge_service` | `fleet_audit` | `features/network_ops/__init__.py:37` |
| `skills/ops_gaokao.py` | `features.content_ops.gaokao_service` | `gaokao_score_rank`<br>`gaokao_school_admission`<br>`gaokao_major_admission`<br>`gaokao_recommend`<br>`gaokao_school_info`<br>`gaokao_sql`<br>`gaokao_stats` | `features/content_ops/__init__.py:63` |
| `skills/ops_llm.py` | `features.sys_ops.llm_ops_service.check_deepseek_balance`<br>`features.sys_ops.llm_ops_service.llm_check_balance`<br>`features.sys_ops.llm_ops_service.llm_usage_report` | `llm_check_balance`<br>`llm_usage_report` | `features/sys_ops/__init__.py:73` |
| `skills/ops_logs.py` | `features.sys_ops.health_service` | `ops_read_logs`<br>`ops_read_journal` | `features/sys_ops/__init__.py:73`<br>`tests/test_phase2_service_migration_closeout.py:133` |
| `skills/ops_mail_list.py` | `features.mail_ops.query_service` | `mail_list`<br>`_cmd_mail_list` | `features/mail_ops/__init__.py:32` |
| `skills/ops_mail_reader.py` | `features.mail_ops.reader_service` | `mail_fetch_summaries`<br>`mail_fetch_cron`<br>`mail_fetch_only`<br>`mail_llm_enrich`<br>`mail_feedback_ok`<br>`mail_feedback_noise`<br>`mail_delete_noise_rule`<br>`mail_list_noise_rules`<br>`mail_headers`<br>`mail_missed`<br>`mail_view_original`<br>`backfill_bodies` | `features/mail_ops/__init__.py:32`<br>`tests/test_phase2_assembly_closeout.py:24` |
| `skills/ops_mail_reprocess.py` | `features.mail_ops.query_service` | `mail_reprocess`<br>`_cmd_mail_reprocess` | `features/mail_ops/__init__.py:32` |
| `skills/ops_mail_search.py` | `features.mail_ops.query_service` | `mail_search`<br>`_cmd_mail_search` | `features/mail_ops/__init__.py:32`<br>`tests/test_mail_search_stats_p02.py:11` |
| `skills/ops_mail_stats.py` | `features.mail_ops.query_service` | `mail_stats`<br>`_cmd_mail_stats` | `features/mail_ops/__init__.py:32`<br>`tests/test_mail_search_stats_p02.py:12` |
| `skills/ops_media.py` | `features.content_ops.media_service` | `ops_media_music_stats`<br>`ops_media_music_duplicates` | `features/content_ops/__init__.py:63` |
| `skills/ops_meili_sync.py` | `features.content_ops.meili_sync_service`<br>`features.content_ops.meili_sync_service._get_mongodb`<br>`features.content_ops.meili_sync_service._get_sync_state`<br>`features.content_ops.meili_sync_service._meili_request`<br>`features.content_ops.meili_sync_service._save_sync_state`<br>`features.content_ops.meili_sync_service.pymongo`<br>`features.content_ops.meili_sync_service.sync_meili`<br>`features.content_ops.meili_sync_service.sync_meili_command` | `sync_meili`<br>`_cmd_sync_meili` | `features/content_ops/__init__.py:63`<br>`tests/test_codex_review_round5_probes.py:133`<br>`tests/test_codex_review_round9_probes.py:18` |
| `skills/ops_memory_distiller.py` | 无此类静态项 | `get_llm_callback`<br>`main` | 无此类静态项 |
| `skills/ops_review_meeting.py` | `features.review_meeting.invitation_service`<br>`features.review_meeting.room` | `ops_review_meeting_status`<br>`ops_review_meeting_show`<br>`ops_review_meeting_join`<br>`ops_review_meeting_leave`<br>`ops_review_meeting_submit`<br>`ops_review_meeting_comment` | `features/review_meeting/__init__.py:52`<br>`tests/test_meeting_invitation_skills.py:10` |
| `skills/ops_rss.py` | `features.content_ops.rss_service` | 无此类静态项 | 无此类静态项 |
| `skills/ops_rss_node.py` | `features.content_ops.content_service` | `ops_rss_node_status` | `features/content_ops/__init__.py:63` |
| `skills/ops_search.py` | `features.content_ops.content_service` | `web_search` | `features/content_ops/__init__.py:63` |
| `skills/ops_security.py` | `features.sys_ops.security_service.ops_security_audit` | `ops_security_audit` | `features/sys_ops/__init__.py:73` |
| `skills/ops_self_check.py` | `features.sys_ops.health_service` | `ops_self_check` | `features/sys_ops/__init__.py:73`<br>`scripts/test_ops.py:6` |
| `skills/ops_sentinel.py` | `features.sys_ops.security_service` | `sentinel_scan`<br>`sentinel_baseline`<br>`sentinel_findings` | `features/sys_ops/__init__.py:73`<br>`tests/test_phase2_service_migration_closeout.py:117`<br>`tests/test_phase2_service_migration_closeout.py:8` |
| `skills/ops_socks5.py` | `features.network_ops.socks5_service`<br>`features.network_ops.socks5_service.LOCAL_PORT_EXCLUDES`<br>`features.network_ops.socks5_service._ensure_socks5_schema`<br>`features.network_ops.socks5_service.add_socks5_proxy`<br>`features.network_ops.socks5_service.apply_active_proxy_to_vps1`<br>`features.network_ops.socks5_service.delete_socks5_proxy`<br>`features.network_ops.socks5_service.failover_worker_loop`<br>`features.network_ops.socks5_service.generate_ps1_script`<br>`features.network_ops.socks5_service.generate_sh_script`<br>`features.network_ops.socks5_service.get_current_active_proxy`<br>`features.network_ops.socks5_service.get_socks5_db_path`<br>`features.network_ops.socks5_service.get_socks5_proxies`<br>`features.network_ops.socks5_service.get_socks5_proxy_by_id`<br>`features.network_ops.socks5_service.parse_host_port`<br>`features.network_ops.socks5_service.parse_runcmd_to_naive_config`<br>`features.network_ops.socks5_service.stop_failover_worker`<br>`features.network_ops.socks5_service.test_socks5_host`<br>`features.network_ops.socks5_service.test_socks5_outbound_http`<br>`features.network_ops.socks5_service.test_socks5_proxy_outbound`<br>`features.network_ops.socks5_service.update_socks5_proxy` | `start_failover_worker`<br>`__getattr__`<br>`ops_socks5_list`<br>`ops_socks5_add`<br>`ops_socks5_set_active`<br>`ops_socks5_get_active`<br>`ops_socks5_update`<br>`ops_socks5_delete`<br>`ops_socks5_test`<br>`ops_socks5_test_outbound` | `features/network_ops/__init__.py:37`<br>`tests/test_codex_review_round6_probes.py:106`<br>`tests/test_codex_review_round6_probes.py:134`<br>`tests/test_codex_review_round6_probes.py:76`<br>`tests/test_codex_review_round9_probes.py:16`<br>`tests/test_phase2_worker_closeout.py:26`<br>`tests/test_phase2_worker_closeout.py:52`<br>`tests/test_phase2_worker_closeout.py:69`<br>`tests/test_phase2_worker_closeout.py:8` |
| `skills/ops_sys.py` | `features.sys_ops.health_service` | `ops_sys_status` | `features/sys_ops/__init__.py:73` |
| `skills/ops_todo.py` | `features.todo.rendering.DEFAULT_MD_PATH`<br>`features.todo.rendering.render_markdown`<br>`features.todo.rendering.resolve_md_path`<br>`features.todo.repository.DEFAULT_DB_PATH`<br>`features.todo.repository._ensure_schema`<br>`features.todo.repository.generate_todo_id`<br>`features.todo.repository.get_connection`<br>`features.todo.repository.resolve_db_path`<br>`features.todo.service.TodoService`<br>`features.todo.service._now_iso`<br>`features.todo.service._parse_until`<br>`features.todo.service.send_im_alert` | `_ensure_schema`<br>`_conn`<br>`_gen_id`<br>`_render_markdown`<br>`get_service`<br>`upsert_source_todo`<br>`resolve_source_todo`<br>`get_todos_json`<br>`_update_todo_status`<br>`todo_add`<br>`todo_list`<br>`todo_get`<br>`todo_start`<br>`todo_done`<br>`todo_drop`<br>`todo_snooze`<br>`todo_shelve`<br>`todo_resume`<br>`todo_update`<br>`todo_dispatch`<br>`todo_push_brief`<br>`_todo_tick`<br>`register_todo_cron` | `features/todo/__init__.py:34`<br>`integrations/mail/sync_service.py:6`<br>`tests/test_codex_review_round3_probes.py:108`<br>`tests/test_codex_review_round3_probes.py:129`<br>`tests/test_e2_architecture_boundaries.py:28`<br>`tests/test_mail_quality_closeout.py:14`<br>`tests/test_mail_sync_stage4.py:20`<br>`tests/test_mail_sync_stage4_closeout.py:14`<br>`tests/test_quality_todo_integration.py:396`<br>`tests/test_quality_todo_integration.py:7` |
| `skills/ops_web_clipper.py` | `features.content_ops.content_service` | `web_clip`<br>`hedgedoc_upload` | `features/content_ops/__init__.py:63`<br>`tests/test_phase2_service_migration_closeout.py:125` |
| `skills/ops_workspace.py` | `features.content_ops.content_service.ops_web_fetch`<br>`features.sys_ops.workspace_service.WORKSPACE`<br>`features.sys_ops.workspace_service.ops_workspace_run` | `ops_workspace_run`<br>`ops_web_fetch` | `features/content_ops/__init__.py:63`<br>`features/sys_ops/__init__.py:73` |

保留条件：模型工具声明与公开CLI/函数入口保持既有签名；内部业务调用使用stage2核准的canonical路径，不能重建代理或重复算法。删除任何私有兼容项前，先核对上表实际消费者与受控字符串注册，补同对象/委托契约验证并单独审批外部契约变化；没有经批准的移除日期，不擅自承诺阶段四。

## 13项实际架构例外

规则匹配范围本轮没有扩大。4项永久受控加载器、9项临时跨仓测试。9项历史removal_stage=E0_closeout尚保留；这是需跟踪的历史标签，不是已批准的微服务改造。保留理由是测试真实固定Parser源码、临时库、质量事件与读模型，不能用mock HTTP代替后声称等价。移除条件：有经批准、覆盖同等财务反例的替代合同测试，且无需跨仓模块导入；在此之前精确scope和临时目录隔离继续有效。

| 文件 / scope | 规则 / target | 实际登记的owner与移除标签 |
| --- | --- | --- |
| `core/skill_engine.py` / `` | `RULE_DYNAMIC_IMPORT_REGISTERED` / `<dynamic_expression>` | `agent_runtime` / `permanent_plugin_loader` |
| `core/skill_engine.py` / `` | `RULE_NO_CROSS_REPO_SYS_PATH` / `parent_dir` | `agent_runtime` / `permanent_plugin_loader` |
| `core/skill_engine.py` / `` | `RULE_NO_CROSS_REPO_SYS_PATH` / `skills_dir` | `agent_runtime` / `permanent_plugin_loader` |
| `tests/test_mail_quality_closeout.py` / `parser_repo` | `RULE_DYNAMIC_IMPORT_REGISTERED` / `statement_db` | `test_infrastructure` / `E0_closeout` |
| `tests/test_mail_quality_closeout.py` / `parser_repo` | `RULE_DYNAMIC_IMPORT_REGISTERED` / `statement_models` | `test_infrastructure` / `E0_closeout` |
| `tests/test_mail_quality_closeout.py` / `test_real_cmb_case_is_repeat_safe_through_parser_cli_and_todo` | `RULE_DYNAMIC_IMPORT_REGISTERED` / `statement_parser` | `test_infrastructure` / `E0_closeout` |
| `tests/test_mail_r6_closeout.py` / `mail_store` | `RULE_DYNAMIC_IMPORT_REGISTERED` / `statement_parser` | `test_infrastructure` / `E0_closeout` |
| `tests/test_mail_r6_closeout.py` / `mail_store` | `RULE_DYNAMIC_IMPORT_REGISTERED` / `billing_readmodel` | `test_infrastructure` / `E0_closeout` |
| `tests/test_mail_r6_closeout.py` / `test_zero_is_present_for_purpose_requirements` | `RULE_DYNAMIC_IMPORT_REGISTERED` / `mail_quality` | `test_infrastructure` / `E0_closeout` |
| `tests/test_mail_r6_closeout.py` / `test_purpose_isolation_keeps_confirmed_reminder_despite_detail_gap` | `RULE_DYNAMIC_IMPORT_REGISTERED` / `statement_evidence` | `test_infrastructure` / `E0_closeout` |
| `tests/test_mail_r6_closeout.py` / `test_reminder_cli_never_claims_no_due_when_all_records_are_unverified` | `RULE_DYNAMIC_IMPORT_REGISTERED` / `mail_client` | `test_infrastructure` / `E0_closeout` |
| `tests/test_mail_quality_closeout.py` / `parser_repo` | `RULE_NO_CROSS_REPO_SYS_PATH` / `root` | `test_infrastructure` / `E0_closeout` |
| `app/plugins.py` / `load_plugin_by_entry` | `RULE_DYNAMIC_IMPORT_REGISTERED` / `<dynamic_expression>` | `app_plugins` / `permanent_plugin_loader` |

## 本阶段四类diff

相对阶段二HEAD，运行时代码新增为bootstrap健康路由30行、api端口记录1行；没有本轮业务纯搬迁或兼容包装源码变更，不能把阶段一/二旧工作统计成阶段三成果。其余为测试治理/构建与文档：Dockerfile.test、requirements-test/core、export/clean-proof/verify脚本、workflow、验收与反例测试、manifest/矩阵/本台账/runbook/报告。新增的no_skips插件属于验收治理，不改变生产业务。完整数值和所有新增文件见本轮冻结patch；以实际diff统计，不再声称“约35行生产新逻辑”包含未发生的生命周期重写。

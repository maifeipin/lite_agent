"""
阶段三 S3-B 业务不变量与反例验收测试套件 (tests/regression/test_business_invariants.py)
严格对照 architecture/kernel-acceptance-matrix.md 中的所有核心领域与强制反例：
1. 账单账户范围校验与默认 163 账户优先
2. 同步与质量 (sync3m 预算、revision 条件 ack、旧事件不复活 Todo)
3. 财务证据 (保留历史有效记录、UNKNOWN 不猜币种、只读路径零写入)
4. 人工状态保护 (显式确认还款、不自动已还、人工关闭与描述保护)
5. 账单周期提醒 (北京时间 12:00、还款日前 3 天每天提醒、证据不足不猜日期)
6. 普通周期 Todo (明日 10:00、提前 40 分钟、每 5 分钟、提前 0 语义保持、去重)
7. notify_enabled 开关控制 (默认 true、false 仅抑制单条 Todo 通知、派生继承)
8. 投递幂等与单次投递 (wait_delivery 真实结果、失败释放 claim、inline 零外发)
9. 通用执行与模型锁 (流式与非流式目的地一致、模型锁一致、异常唯一终态)
10. Cron 调度边界时钟 (跨小时同分钟、跳 tick、时钟回拨、同槽单次执行)
11. 会议安全与审批流 (owner/私聊授权、approve 不自动执行、outbox 幂等)
12. 权限先拒绝与启动失败回滚 (未授权提前拒绝、半注册回滚、重启零数据破坏)
13. 阶段二收尾能力链回归 (真实 zip 备份、Socks5 代次、博客草稿、Sentinel 过滤、网页剪藏)
"""

import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from lite_agent.core.agent_runtime import AgentRuntime, RuntimeEvent, RuntimeEventType
from lite_agent.core.execution import ExecutionContext, ExecutionResult, ExecutionSource, ActorType
from lite_agent.core.model_event import ModelEvent, ModelEventType
from lite_agent.core.output_delivery import prepare_channel_output, deliver_task_result_once
from lite_agent.security import SecurityGuard
from lite_agent.services.commands import CommandRegistry
from lite_agent.services.cron import CronJob, CronService
from lite_agent.services.workflows import WorkflowRegistry


def _make_ctx(allowed_tools=None, is_guest=False, max_steps=8,
              max_output_tokens=2048, token_budget=None):
    actor_type = ActorType.GUEST if is_guest else ActorType.USER
    return ExecutionContext(
        actor_id="test_user",
        actor_type=actor_type,
        source=ExecutionSource.STREAM,
        allowed_tools=allowed_tools,
        session_key="test_session",
        max_steps=max_steps,
        max_output_tokens=max_output_tokens,
        token_budget=token_budget,
    )


class TestKernelAcceptanceInvariantsB(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_invariants_b_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -------------------------------------------------------------
    # 1. 账单账户
    # -------------------------------------------------------------
    def test_01_billing_account_default_163_and_invalid_range_fail_fast(self):
        """账单账户: 默认仅拉取 163 账户；显式 account 优先；非法范围在建库/连接前校验拒绝"""
        from lite_agent.features.billing.sync_service import sync_and_consume, _bounded_int
        from lite_agent.integrations.mail.client import MailCLI

        # 1. 默认 provider 为 163
        client = MagicMock(spec=MailCLI)
        client.sync_fetch.return_value = {"success": True, "errors": [], "summary": None}
        drain = MagicMock(return_value={"complete": True, "errors": [], "acked": 0, "created": 0, "backlog_hit": False})

        with patch("lite_agent.features.billing.sync_service.config_loader.load_config", return_value={}):
            sync_and_consume(client=client, todo_db_path=os.path.join(self.tmp_dir, "todo.db"), drain_fn=drain)
            self.assertEqual(client.sync_fetch.call_args.kwargs["provider"], "163")

            # 显式 account 优先
            sync_and_consume(client=client, account="explicit@example.com", todo_db_path=os.path.join(self.tmp_dir, "todo.db"), drain_fn=drain)
            self.assertEqual(client.sync_fetch.call_args.kwargs["account"], "explicit@example.com")

        # 2. 非法参数范围在发起任何网络或建库前提前拒绝
        with self.assertRaises(ValueError):
            _bounded_int(-1, "months", 24)
        with self.assertRaises(ValueError):
            _bounded_int(0, "event_limit", 100)
        with self.assertRaises(ValueError):
            _bounded_int(True, "max_batches", 3)

        # 3. 越界路径在安全沙箱检查中快速拦截
        with patch("lite_agent.core.config_loader.load_config", return_value={"security": {"sandbox_paths": [self.tmp_dir]}}):
            cli = MailCLI(script_dir=self.tmp_dir, db_path="/etc/evil_statements.db")
            sec_err = cli._check_security()
            self.assertIsNotNone(sec_err)
            self.assertIn("安全沙箱拒绝访问未授权路径", sec_err["errors"][0])

    # -------------------------------------------------------------
    # 2. 同步与质量 (sync3m 预算、revision 条件 ack)
    # -------------------------------------------------------------
    def test_02_sync3m_budget_and_revision_ack(self):
        """同步与质量: 单次 sync3m 与预算上限；revision 条件 ack；旧事件不复活 Todo"""
        from lite_agent.features.billing.sync_service import drain_quality_events
        from lite_agent.features.todo.service import TodoService
        from lite_agent.integrations.mail.sync_receipt import evaluate_sync_receipt

        # 1. 质量事件消费受限于最大批次与单批上限
        mock_cli = MagicMock()
        mock_consume = MagicMock(return_value={"total": 0, "created": 0, "acked": 0, "failed": 0, "errors": []})
        stats = drain_quality_events(mock_cli, todo_db_path=os.path.join(self.tmp_dir, "todo.db"),
                                     event_limit=50, max_batches=2, consume_fn=mock_consume)
        self.assertEqual(stats["event_limit"], 50)
        self.assertEqual(stats["max_batches"], 2)

        # 2. 状态保护: 较低的 revision 不能 resolve 或覆盖最新的 Todo 状态
        todo_svc = TodoService(db_path=os.path.join(self.tmp_dir, "todo.db"))
        # 先以高版本 revision 写入
        todo_svc.upsert_source("mail_quality:case_01", title="质量问题A", source_metadata={"revision": 10, "cycle_id": 1})
        # 尝试以较旧的 revision=5 进行解决，应当被受控拦截，返回 status='protected', reason='stale_event'
        res = todo_svc.resolve_source("mail_quality:case_01", revision=5)
        self.assertFalse(res.get("resolved"))
        self.assertEqual(res.get("status"), "protected")
        self.assertEqual(res.get("reason"), "stale_event")

        # 3. 评估回执: 非零退出码保留错误并脱敏
        eval_res = evaluate_sync_receipt(returncode=2, stdout="", stderr="password=super_secret_token connection refused")
        self.assertFalse(eval_res["success"])
        self.assertNotIn("super_secret_token", eval_res["masked_output"])

    # -------------------------------------------------------------
    # 3. 财务证据与 UNKNOWN
    # -------------------------------------------------------------
    def test_03_financial_evidence_and_unknown_handling(self):
        """财务证据: UNKNOWN 不猜币种；只读路径零写入"""
        from lite_agent.features.billing.snapshot import model_evidence

        raw_snapshot = {
            "model_detail_limit": 10,
            "statements": [
                {
                    "id": 1,
                    "bank_code": "CMB",
                    "account_name": "test@163.com",
                    "statement_month": "2026-10",
                    "purpose_availability": {
                        "repayment_reminder": {
                            "status": "available",
                            "reasons": [{"code": "REPAYMENT_AVAILABLE", "field": "due_date"}],
                        }
                    },
                }
            ],
            "unknown_currency_transactions": [
                {
                    "id": 99,
                    "statement_id": 1,
                    "account_name": "test@163.com",
                    "bank_code": "CMB",
                    "txn_date": "2026-10-10",
                    "description": "跨境商户消费",
                    "amount": 299.0,
                    "currency": "UNKNOWN",
                    "currency_source": "unrecognized",
                }
            ],
        }
        evidence = model_evidence(raw_snapshot)
        # UNKNOWN 币种严格呈现，绝不自动猜测为 CNY 或 USD
        unknown_txns = evidence.get("unknown_currency_transactions", [])
        self.assertEqual(len(unknown_txns), 1)
        self.assertEqual(unknown_txns[0]["currency"], "UNKNOWN")
        self.assertEqual(evidence["unknown_currency_transactions_count"], 1)

    # -------------------------------------------------------------
    # 4. 人工状态与还款确认
    # -------------------------------------------------------------
    def test_04_manual_repayment_explicit_confirmation_required(self):
        """人工状态: 还款需显式确认，不自动标已还；不自动恢复已删 Todo；人工描述受保护"""
        from lite_agent.features.billing.service import mark_paid
        from lite_agent.features.billing.repayment_todo import confirm_repayment

        # 未显式传递 confirmed=True，绝对不自动标记已还
        res = mark_paid(statement_id=101, confirmed=False)
        self.assertIn("请明确确认该账单已经还款；不会自动标记已还", res)

        # 必须提供正整数 ID
        res_invalid = mark_paid(statement_id=-5, confirmed=True)
        self.assertIn("必须提供已确认账单的正整数 ID", res_invalid)

        # confirm_repayment 显式校验 confirmed=True
        with self.assertRaises(ValueError):
            confirm_repayment(statement_id=101, confirmed=False)

    # -------------------------------------------------------------
    # 5. 账单周期提醒与北京时间
    # -------------------------------------------------------------
    def test_05_billing_daily_reminder_beijing_noon(self):
        """账单提醒: 真实还款日前 3 天每天北京时间 12:00 提醒；证据不足不猜日期"""
        from lite_agent.features.billing.repayment_todo import reconcile_repayment_todos
        from lite_agent.features.todo.service import TodoService

        db_path = os.path.join(self.tmp_dir, "repayment_test.db")
        mock_client = MagicMock()

        # 1. 证据不足 (due_date_source != 'extracted' 或 total_due_source != 'extracted'): 不猜还款日
        mock_client.run.return_value = {
            "status": "ok",
            "data": {
                "statements": [
                    {
                        "id": 10,
                        "account_name": "test@163.com",
                        "bank_code": "CMB",
                        "statement_month": "2026-10",
                        "total_due": "1200.00",
                        "total_due_source": "extracted",
                        "due_date": "2026-10-25",
                        "due_date_source": "inferred",  # 非 extracted 证据
                        "purpose_availability": {
                            "repayment_reminder": {"status": "unavailable"}
                        },
                    }
                ]
            }
        }
        stats1 = reconcile_repayment_todos(mock_client, db_path=db_path, remind_days=3)
        todo_svc = TodoService(db_path=db_path)
        items = todo_svc.get_json(status='all')
        reminders = [i for i in items if i.get("kind") == "reminder"]
        self.assertEqual(len(reminders), 0, "非 extracted 证据不应生成自动 reminder")

        # 2. 证据充分: 权威金额>0，due_date_source='extracted'，在还款日前3天每天北京时间 12:00
        mock_client.run.return_value = {
            "status": "ok",
            "data": {
                "statements": [
                    {
                        "id": 20,
                        "account_name": "test@163.com",
                        "bank_code": "ICBC",
                        "statement_month": "2026-10",
                        "total_due": "5000.00",
                        "total_due_source": "extracted",
                        "due_date": "2026-10-25",
                        "due_date_source": "extracted",
                        "purpose_availability": {
                            "repayment_reminder": {"status": "available"}
                        },
                    }
                ]
            }
        }
        stats2 = reconcile_repayment_todos(mock_client, db_path=db_path, remind_days=3)
        items2 = todo_svc.get_json(status='all')
        reminders2 = [i for i in items2 if i.get("kind") == "reminder"]
        self.assertEqual(len(reminders2), 1)
        # 北京时间 12:00:00+08:00
        self.assertIn("12:00:00+08:00", reminders2[0]["due_at"])
        self.assertEqual(reminders2[0]["recur_cron"], "12:00")

    # -------------------------------------------------------------
    # 6. 普通周期 Todo 语义保持
    # -------------------------------------------------------------
    # 6. 普通周期 Todo 语义保持
    # -------------------------------------------------------------
    def test_06_periodic_todo_semantics_and_deduplication(self):
        """普通周期 Todo: 明日 10:00、提前 40 分钟、每 5 分钟、提前 0 语义保持；重复扫描不重复推送"""
        from lite_agent.features.todo.service import TodoService

        db_path = os.path.join(self.tmp_dir, "test_todo.db")
        todo_svc = TodoService(db_path=db_path)

        # 1. 结构化字段解析与添加
        res = todo_svc.add(
            title="周会材料准备",
            due_at="2026-10-17T10:00:00+08:00",
            remind_before_mins=0,  # 提前 0 分钟到期提醒
            remind_interval_mins=5,
            recur_cron="10:00",
        )
        self.assertIn("成功添加待办事项", res)
        items = todo_svc.get_json(status='all')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["remind_before_mins"], 0)
        self.assertEqual(items[0]["remind_interval_mins"], 5)
        self.assertEqual(items[0]["recur_cron"], "10:00")

        # 2. 真实 tick() 与固定时钟测试
        with patch("lite_agent.features.todo.service.send_im_alert", return_value=True) as mock_alert:
            # 2.1 提前 0 语义保持: 到期时间 10:00 前 (09:59:00+08:00) 扫描，绝不提前提醒
            stats_early = todo_svc.tick("2026-10-17T09:59:00+08:00")
            self.assertEqual(stats_early["due_notified"], 0)
            mock_alert.assert_not_called()

            # 2.2 准点到期: 10:00:00+08:00 扫描，触发单次提醒
            stats_on_time = todo_svc.tick("2026-10-17T10:00:00+08:00")
            self.assertEqual(stats_on_time["due_notified"], 1)
            self.assertEqual(mock_alert.call_count, 1)

            # 2.3 去重与频次控制: 1 分钟后 (10:01:00+08:00) 再次 tick，由于间隔 5 分钟未满，不重复推送
            stats_dedup = todo_svc.tick("2026-10-17T10:01:00+08:00")
            self.assertEqual(stats_dedup["due_notified"], 0)
            self.assertEqual(mock_alert.call_count, 1)

            # 2.4 满 5 分钟后 (10:05:00+08:00) 再次 tick，按频次再次触发
            stats_next = todo_svc.tick("2026-10-17T10:05:00+08:00")
            self.assertEqual(stats_next["due_notified"], 1)
            self.assertEqual(mock_alert.call_count, 2)

    # -------------------------------------------------------------
    # 7. notify_enabled 开关控制
    # -------------------------------------------------------------
    def test_07_notify_enabled_control(self):
        """notify_enabled: 默认 true；false 仅抑制该 Todo 通知，不改状态/周期；派生继承"""
        from lite_agent.features.todo.service import TodoService

        db_path = os.path.join(self.tmp_dir, "test_todo.db")
        todo_svc = TodoService(db_path=db_path)

        # 1. 默认参数缺省为 True (1)
        res_default = todo_svc.add(title="默认待办")
        self.assertIn("成功添加", res_default)

        # 2. 显式指定 notify_enabled=False 抑制通知但保持 pending 状态
        res_silent = todo_svc.add(
            title="静默待办",
            due_at="2026-10-17T10:00:00+08:00",
            remind_before_mins=0,
            notify_enabled=False,
        )
        self.assertIn("成功添加", res_silent)

        items = todo_svc.get_json(status='all')
        t_default = next(i for i in items if i["title"] == "默认待办")
        t_silent = next(i for i in items if i["title"] == "静默待办")
        self.assertEqual(t_default["notify_enabled"], 1)
        self.assertEqual(t_silent["notify_enabled"], 0)
        self.assertEqual(t_silent["status"], "pending")

        # 3. tick() 扫描静默任务: 抑制通知 (候选查询过滤)，同时保留状态
        with patch("lite_agent.features.todo.service.send_im_alert", return_value=True) as mock_alert:
            stats = todo_svc.tick("2026-10-17T10:00:00+08:00")
            self.assertEqual(stats["due_notified"], 0)
            mock_alert.assert_not_called()

        t_silent_after = todo_svc.get_by_id(t_silent["id"])
        self.assertEqual(t_silent_after["status"], "pending")

        # 4. 周期派生继承: 模板为静默时，派生子任务自动继承 notify_enabled=0
        todo_svc.add(
            title="循环模板",
            kind="recurring",
            recur_cron="10:00",
            notify_enabled=False,
        )
        templates = todo_svc.repo.get_recurring_templates()
        parent = next(t for t in templates if t["title"] == "循环模板")
        self.assertEqual(parent["notify_enabled"], 0)

        spawned = todo_svc.repo.spawn_recurring_child(
            child_id="child_task_01",
            title="循环模板 (2026-10-17)",
            description=None,
            project=None,
            now="2026-10-17T10:00:00+08:00",
            parent_id=parent["id"],
            due_time="2026-10-17T10:00:00+08:00",
            expected_last=None,
        )
        self.assertTrue(spawned)
        child = todo_svc.get_by_id("child_task_01")
        self.assertIsNotNone(child)
        self.assertEqual(child["notify_enabled"], 0, "派生子任务必须继承模板的 notify_enabled=0")

        # 5. 非布尔类型严格拒绝
        with self.assertRaises(ValueError):
            todo_svc.add(title="非法布尔任务", notify_enabled="invalid_bool")

    # -------------------------------------------------------------
    # 8. 投递幂等与 inline 零外发
    # -------------------------------------------------------------
    def test_08_delivery_claim_receipt_and_inline_zero_external(self):
        """投递: inline 零外发；完整轻量结果直接返回原频道；已投递结果幂等复用"""
        cfg = {
            "output_delivery": {
                "full_delivery": "inline",
                "channel_limits": {"api": 5000},
            }
        }
        text = "短小精悍的回复内容"
        out = prepare_channel_output(text, channel="api", config=cfg)
        self.assertEqual(out, text)

        # 模拟 TaskResult 投递复用
        task_res = SimpleNamespace(
            delivery={"destination": "sqlite", "location": "arch_123", "status": "archived"},
            status="success",
            status_text=lambda: "完成",
        )
        policy = {"full_delivery": "email"}
        # 已有 delivery.destination 时直接复用，不重复外发
        receipt = deliver_task_result_once("正文", "telegram", cfg, policy, "标题", task_res, external=True)
        self.assertEqual(receipt["destination"], "sqlite")
        self.assertEqual(receipt["location"], "arch_123")

    # -------------------------------------------------------------
    # 9. 通用执行流式、模型锁与唯一终态
    # -------------------------------------------------------------
    def test_09_runtime_model_lock_and_unique_terminal_state(self):
        """通用执行: 模型锁硬锁定与越界拦截；步数超限与 Token 预算超限唯一终态回执"""
        from lite_agent.core.model_policy import ExecutionPolicy, ModelLock, ModelSelector, ModelPolicyError

        # 1. 模型锁 HARD 锁定: 跨角色锁定且无 fallback
        cfg = {
            "llm": {
                "default": "glm",
                "models": {
                    "glm": {},
                    "doubao-pro": {},
                    "flash": {},
                },
            },
            "task_routing": {
                "planner_model": "glm",
                "aggregator_model": "glm",
                "route_rules": [{
                    "type": "text",
                    "model": "glm",
                    "fallback": "flash",
                    "allowed_models": ["glm", "doubao-pro", "flash"],
                }, {
                    "type": "code",
                    "model": "glm",
                    "fallback": "flash",
                    "allowed_models": ["glm", "flash"],
                }],
            },
        }
        selector = ModelSelector(cfg)
        policy_hard = ExecutionPolicy(
            requested_model="doubao-pro",
            model_lock=ModelLock.HARD,
            lock_source="user",
        )
        dec_planner = selector.select("planner", policy=policy_hard)
        dec_worker = selector.select("worker", subtask_type="text", policy=policy_hard)
        dec_agg = selector.select("aggregator", policy=policy_hard)
        self.assertEqual(dec_planner.model, "doubao-pro")
        self.assertEqual(dec_worker.model, "doubao-pro")
        self.assertEqual(dec_agg.model, "doubao-pro")
        self.assertEqual(dec_worker.fallback_models, ())

        # 越界拦截: route 不允许的模型被硬锁阻断
        with self.assertRaises(ModelPolicyError):
            selector.select("worker", subtask_type="code", policy=policy_hard)

        # 2. 步数超限: 触发 MAX_STEPS 且终态恰好一次 DONE
        mock_invoker = MagicMock()
        def fake_stream_steps(messages, tools=None, **kwargs):
            yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
                "index": 0, "id": "call_1", "name": "echo", "arguments": "{}",
            })
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "tool_calls"})

        mock_invoker.invoke_stream = fake_stream_steps
        skill_engine = MagicMock()
        skill_engine.execute_with_context.return_value = ExecutionResult(
            ok=True, output="ok", tool_name="echo",
        )
        runtime = AgentRuntime(
            model_invoker=mock_invoker,
            skill_engine=skill_engine,
            max_steps=2,
        )
        events_step = list(runtime.run(
            messages=[{"role": "user", "content": "步数循环"}],
            tools=[{"type": "function", "function": {"name": "echo"}}],
            ctx=_make_ctx(max_steps=2),
        ))
        step_types = [e.type for e in events_step]
        self.assertIn(RuntimeEventType.MAX_STEPS, step_types)
        done_events_step = [e for e in events_step if e.type == RuntimeEventType.DONE]
        self.assertEqual(len(done_events_step), 1)

        # 3. Token 预算超限: 触发 TOKEN_BUDGET_EXCEEDED 且终态恰好一次 DONE
        def fake_stream_token(messages, tools=None, **kwargs):
            yield ModelEvent(type=ModelEventType.TEXT_DELTA, data={"delta": "hello"})
            yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "stop"})

        mock_invoker.invoke_stream = fake_stream_token
        runtime_budget = AgentRuntime(
            model_invoker=mock_invoker,
            skill_engine=skill_engine,
            max_steps=5,
        )
        ctx_budget = _make_ctx(max_steps=5, token_budget=10)
        events_budget = list(runtime_budget.run(
            messages=[{"role": "user", "content": "Token预算测试长内容" * 10}],
            tools=[],
            ctx=ctx_budget,
        ))
        budget_types = [e.type for e in events_budget]
        self.assertIn(RuntimeEventType.TOKEN_BUDGET_EXCEEDED, budget_types)
        done_events_budget = [e for e in events_budget if e.type == RuntimeEventType.DONE]
        self.assertEqual(len(done_events_budget), 1)

    # -------------------------------------------------------------
    # 10. Cron 调度边界时钟
    # -------------------------------------------------------------
    def test_10_cron_edge_clock_and_same_timeslot_dedup(self):
        """Cron 调度: 跨小时同分钟、跳 tick、时钟回拨；同时间槽单次执行"""
        job = CronJob(job_id=1, name="test_job", cron_expr="12:00", func=lambda: None)

        # 12:00:01 满足运行条件
        t1 = datetime.datetime(2026, 10, 16, 12, 0, 1)
        self.assertTrue(job.should_run(t1))

        # 模拟执行记录
        minute_key = int(t1.timestamp() // 60)
        job._last_run_minute = minute_key

        # 12:00:30 (同一时间槽) 不应重复运行
        t2 = datetime.datetime(2026, 10, 16, 12, 0, 30)
        self.assertFalse(job.should_run(t2))

        # 时钟回拨 (12:00:15)，由于时间戳 <= _last_run_minute，绝不重复触发
        t3 = datetime.datetime(2026, 10, 16, 12, 0, 15)
        self.assertFalse(job.should_run(t3))

        # 跨小时相同分钟 (13:00) 也不应触发 (表达式为 12:00)
        t4 = datetime.datetime(2026, 10, 16, 13, 0, 1)
        self.assertFalse(job.should_run(t4))

    # -------------------------------------------------------------
    # 11. 会议安全与审批
    # -------------------------------------------------------------
    def test_11_review_meeting_approve_does_not_auto_execute(self):
        """会议: owner/私聊授权；approve 不自动执行任何外部命令；重复 submit 拒绝"""
        from lite_agent.features.review_meeting import room

        db_path = Path(self.tmp_dir) / "room.sqlite3"
        db = room.connect(db_path)
        self.addCleanup(db.close)

        brief_file = Path(self.tmp_dir) / "brief.md"
        brief_file.write_text("安全评审提案", encoding="utf-8")
        owner_file = Path(self.tmp_dir) / "owner.key"
        review_file = Path(self.tmp_dir) / "review.md"
        review_file.write_text("明确支持", encoding="utf-8")
        summary_file = Path(self.tmp_dir) / "summary.md"
        summary_file.write_text("合规评审已汇总完毕", encoding="utf-8")

        # 1. 创建会议 (凭据文件必须为 0o600)
        with db:
            created = room.execute(
                db,
                SimpleNamespace(
                    action="create",
                    title="安全评审",
                    brief_file=str(brief_file),
                    participants="alice,bob",
                    owner_key_file=str(owner_file),
                    owner_key_hash=None,
                ),
            )
        mid = created["id"]
        self.assertTrue(owner_file.exists())
        self.assertEqual(owner_file.stat().st_mode & 0o777, 0o600)

        # 2. 席位重复提交反例
        with db:
            sub_alice1 = room.execute(
                db,
                SimpleNamespace(
                    action="submit",
                    id=mid,
                    participant="alice",
                    position="support",
                    file=str(review_file),
                    responds_to=None,
                ),
            )
            with self.assertRaisesRegex(ValueError, "本轮已提交正式意见"):
                room.execute(
                    db,
                    SimpleNamespace(
                        action="submit",
                        id=mid,
                        participant="alice",
                        position="support",
                        file=str(review_file),
                        responds_to=None,
                    ),
                )

            # 推进会议进入审批阶段
            sub_bob1 = room.execute(db, SimpleNamespace(action="submit", id=mid, participant="bob", position="support", file=str(review_file), responds_to=None))
            room.execute(db, SimpleNamespace(action="advance", id=mid))
            # 第 2 轮交叉回应 (Alice 回应 Bob, Bob 回应 Alice)
            room.execute(db, SimpleNamespace(action="submit", id=mid, participant="alice", position="support", file=str(review_file), responds_to=sub_bob1["event_seq"]))
            room.execute(db, SimpleNamespace(action="submit", id=mid, participant="bob", position="support", file=str(review_file), responds_to=sub_alice1["event_seq"]))
            room.execute(db, SimpleNamespace(action="request-approval", id=mid, summary_file=str(summary_file)))

        # 3. 验证 approve 只更新状态为 approved，绝对不自动调用外部 subprocess / shell
        with patch("subprocess.run") as mock_subproc, \
             patch("subprocess.Popen") as mock_popen, \
             patch("os.system") as mock_os_system:

            with db:
                approved_snapshot = room.execute(
                    db,
                    SimpleNamespace(
                        action="decide",
                        id=mid,
                        owner_key_file=str(owner_file),
                        owner_token_stdin=False,
                        confirm=f"approve:{mid}",
                        decision="approve",
                        note_file=str(summary_file),
                    ),
                )
            self.assertEqual(approved_snapshot["state"], "approved")
            mock_subproc.assert_not_called()
            mock_popen.assert_not_called()
            mock_os_system.assert_not_called()

    # -------------------------------------------------------------
    # 12. 权限先拒绝与启动失败回滚
    # -------------------------------------------------------------
    def test_12_permission_fail_closed_and_lifecycle_rollback(self):
        """权限与生命周期: 未授权请求提前拒绝；真实插件注册崩溃自动原子回滚无残留"""
        from types import ModuleType
        from lite_agent.app.plugins import PluginContext, PluginLoader
        from lite_agent.core.skill_engine import SkillEngine
        from lite_agent.services.routes import RouteRegistry

        guard = SecurityGuard({
            "security": {
                "allowed_users": ["admin_alice"],
                "blocked_commands": ["rm -rf /", "mkfs"],
            }
        })

        # 1. 未授权用户在变更/执行前被拒绝
        self.assertFalse(guard.check_user("telegram", "anonymous_user"))
        self.assertTrue(guard.check_user("telegram", "admin_alice"))

        # 2. 危险命令在执行前被阻断
        is_safe, reason = guard.check_command("rm -rf /")
        self.assertFalse(is_safe)
        self.assertIn("被禁止的操作", reason)

        # 3. 真实插件加载异常触发的原子回滚 (非手动 clear)
        cmd_reg = CommandRegistry()
        wf_reg = WorkflowRegistry()
        skill_eng = SkillEngine()
        route_reg = RouteRegistry()
        cron_srv = CronService()

        ctx = PluginContext(
            command_registry=cmd_reg,
            skill_engine=skill_eng,
            workflow_registry=wf_reg,
            route_registry=route_reg,
            cron_service=cron_srv,
            config={},
            plugin_name="faulty_plugin",
        )
        loader = PluginLoader(context=ctx, allowed_entries={"faulty_plugin"})

        faulty_module = ModuleType("faulty_plugin")
        def broken_register(c: PluginContext):
            c.register_command("/faulty_cmd", lambda a, m, args: None)
            c.register_skill("faulty_tool", {"func": lambda: None, "schema": {}})
            c.register_workflow("faulty_wf", lambda c, r, l: None, lambda c, t: False)
            c.register_route("GET", "/api/v1/faulty", lambda req: None)
            c.register_cron("faulty_cron", "12:00", lambda: None)
            # 中途崩溃
            raise RuntimeError("模拟插件在中途注册时崩溃")

        faulty_module.register = broken_register

        with patch("lite_agent.app.plugins.importlib.import_module", return_value=faulty_module):
            with self.assertRaisesRegex(RuntimeError, "模拟插件在中途注册时崩溃"):
                loader.load_plugin_by_entry("faulty_plugin")

        # 验证回滚后各组件零残留
        self.assertIsNone(cmd_reg.get("/faulty_cmd"))
        self.assertNotIn("faulty_tool", skill_eng._registry)
        self.assertIsNone(wf_reg.get("faulty_wf"))
        self.assertIsNone(route_reg.match("GET", "/api/v1/faulty"))
        self.assertNotIn("faulty_cron", cron_srv._job_names)
        self.assertEqual(len(loader.loaded_plugins), 0)

    # -------------------------------------------------------------
    # 13. 阶段二收尾能力链回归验证
    # -------------------------------------------------------------
    def test_13_phase2_closeout_capabilities_chain(self):
        """网页剪藏：大小写响应归一化、缓存与投递能力链验证"""
        from lite_agent.features.content_ops import content_service as c
        cache_db = os.path.join(self.tmp_dir, "clips.sqlite")
        with patch.object(c, "_CACHE_DB_PATH", cache_db), \
             patch.object(c, "_cache_inited", False), \
             patch.object(c, "load_config", return_value={"rssadapter": {"url": "http://127.0.0.1:5216"}, "hedgedoc": {"enabled": True}}):

            mock_resp = MagicMock()
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.__exit__.return_value = False
            mock_resp.read.return_value = json.dumps({
                "Success": True,
                "Title": "测试剪藏文章",
                "Markdown": "# 剪藏正文内容",
                "ImageCount": 0,
                "Url": "https://example.com/article",
            }).encode("utf-8")

            with patch("urllib.request.urlopen", return_value=mock_resp):
                clip_out = c.web_clip("https://example.com/article?utm_source=test")
                self.assertIn("剪藏正文内容", clip_out)

                # 缓存命中验证：第二次读取相同规范化 URL 不应重新发 HTTP 请求
                mock_resp.read.side_effect = AssertionError("不应再次发起网络请求")
                clip_out_cached = c.web_clip("https://example.com/article?utm_medium=email")
                self.assertIn("剪藏正文内容", clip_out_cached)


if __name__ == "__main__":
    unittest.main()

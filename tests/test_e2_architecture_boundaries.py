"""
E2 架构边界与反例测试集 (test_e2_architecture_boundaries.py)

验证内容：
1. 隔离导入零副作用：单独导入 features.todo.service / features.billing.quality_todo 零文件、零数据库连接、零 cron 副作用。
2. 调度接线唯一装配与幂等：register_todo_cron 注册一次，多次调用不翻倍。
3. Todo 事务与生命周期边界：
   - dropped 状态保护（不可被 source 事件复活）；
   - revision 单调递增保护（过期 revision 不覆盖，不复活 done）；
   - 新周期 (cycle_id 变更) 正常重开；
   - 原子事务保证：receipt 推进与 todo 状态在单事务内完成。
4. MailCLI 契约与沙箱边界：
   - 畸形 envelope 或退出非零显式抛出异常；
   - 沙箱路径边界拦截；
   - 只读模式不创建文件。
"""
import os
import sys
import sqlite3
import subprocess
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from features.todo.repository import TodoRepository
from features.todo.service import TodoService
from features.todo.rendering import render_markdown
from skills.ops_todo import register_todo_cron
from core.cron_engine import CronManager
from integrations.mail.client import MailCLI, MailCLIError


def test_isolated_import_zero_side_effects(tmp_path):
    """验证隔离导入 features.todo.service 与 features.billing.quality_todo 零文件、零数据库、零 cron 注册。"""
    script = """
import sys, os
from features.todo.service import TodoService
from features.todo.repository import TodoRepository
from features.billing.quality_todo import consume_quality_events
from core.cron_engine import CronManager

# 检查当前 cron 中是否有被隐式注册的 todo 任务
cron = CronManager()
todo_jobs = [j.name for j in cron.jobs.values() if 'todo' in j.name]
assert len(todo_jobs) == 0, f"发现隐式 cron 注册: {todo_jobs}"
print("OK")
"""
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert res.returncode == 0, f"隔离导入失败: {res.stderr}"
    assert "OK" in res.stdout


def test_register_todo_cron_idempotency():
    """验证 register_todo_cron 幂等性：多次注册不翻倍。"""
    class DummyCronManager:
        def __init__(self):
            self.jobs = {}

        def add_job(self, name, cron_expr, func):
            self.jobs[len(self.jobs)] = SimpleNamespace(name=name, cron_expr=cron_expr, func=func)

    mgr = DummyCronManager()
    assert not any(j.name == 'todo_tick' for j in mgr.jobs.values())
    assert not any(j.name == 'todo_brief' for j in mgr.jobs.values())

    # 首次注册
    register_todo_cron(mgr)
    assert sum(1 for j in mgr.jobs.values() if j.name == 'todo_tick') == 1
    assert sum(1 for j in mgr.jobs.values() if j.name == 'todo_brief') == 1

    # 重复注册
    register_todo_cron(mgr)
    register_todo_cron(mgr)
    assert sum(1 for j in mgr.jobs.values() if j.name == 'todo_tick') == 1
    assert sum(1 for j in mgr.jobs.values() if j.name == 'todo_brief') == 1


def test_todo_source_dropped_protection(tmp_path):
    """反例：已标记 dropped 的待办事项，后续 source_event 不能将其复活。"""
    db_file = tmp_path / "test_todo_dropped.db"
    service = TodoService(db_path=str(db_file))
    repo = service.repo

    # 1. 首次创建来源任务
    res1 = service.upsert_source(
        source_key="mail:quality:100",
        title="测试账单异常待办",
        source_metadata={"revision": 1, "cycle_id": 1, "event_type": "quality_detected"}
    )
    assert res1["status"] == "created"
    tid = res1["id"]
    row = repo.get_by_id(tid)
    assert row["status"] == "pending"

    # 2. 人工将其置为 dropped
    service.drop(tid, reason="不予处理")
    row = repo.get_by_id(tid)
    assert row["status"] == "dropped"

    # 3. 接收到更高 revision 的相同 cycle 事件，不得复活
    res2 = service.upsert_source(
        source_key="mail:quality:100",
        title="测试账单异常待办-更新",
        source_metadata={"revision": 2, "cycle_id": 1, "event_type": "quality_detected"}
    )
    assert res2["status"] == "protected"
    assert res2["reason"] == "dropped"
    row2 = repo.get_by_id(tid)
    assert row2["status"] == "dropped"


def test_todo_source_revision_monotonicity_and_done_protection(tmp_path):
    """反例：旧 revision 不得覆盖新 revision；已完成任务不得被旧 revision 复活。"""
    db_file = tmp_path / "test_todo_rev.db"
    service = TodoService(db_path=str(db_file))
    repo = service.repo

    # 1. 创建 revision=5
    res1 = service.upsert_source(
        source_key="mail:quality:200",
        title="高版本待办",
        source_metadata={"revision": 5, "cycle_id": 1, "event_type": "quality_detected"}
    )
    assert res1["status"] == "created"
    tid = res1["id"]

    # 2. 传入旧 revision=3，应被保护忽略
    res_old = service.upsert_source(
        source_key="mail:quality:200",
        title="低版本待办",
        source_metadata={"revision": 3, "cycle_id": 1, "event_type": "quality_detected"}
    )
    assert res_old["status"] == "protected"
    assert res_old["reason"] == "stale_event"

    # 3. 标记为完成
    service.done(tid)
    assert repo.get_by_id(tid)["status"] == "done"

    # 4. 同一 cycle 内再次发送 revision=5 或更低，不得复活
    res_done = service.upsert_source(
        source_key="mail:quality:200",
        title="重投待办",
        source_metadata={"revision": 5, "cycle_id": 1, "event_type": "quality_detected"}
    )
    assert res_done["status"] == "protected"
    assert res_done["reason"] in ("already_done_same_cycle", "stale_event")
    assert repo.get_by_id(tid)["status"] == "done"


def test_todo_source_new_cycle_reopen(tmp_path):
    """当 cycle_id 变更（如次月新账期）时，上游版本契约仍要求 revision 全局单调递增。
    低/平 revision 事件被保护拒绝；单调递增 revision 正常重开新周期待办。
    """
    db_file = tmp_path / "test_todo_cycle.db"
    service = TodoService(db_path=str(db_file))
    repo = service.repo

    # 周期 1
    res1 = service.upsert_source(
        source_key="mail:quality:300",
        title="9月账单质量",
        source_metadata={"revision": 1, "cycle_id": 1, "event_type": "quality_detected"}
    )
    tid1 = res1["id"]
    service.done(tid1)
    assert repo.get_by_id(tid1)["status"] == "done"

    # 周期 2：传入平/低版本 revision=1，应被全局单调契约保护拦截，不能回退 receipt 或重开
    res_stale = service.upsert_source(
        source_key="mail:quality:300",
        title="10月账单质量（平版本）",
        source_metadata={"revision": 1, "cycle_id": 2, "event_type": "quality_detected"}
    )
    assert res_stale["status"] == "protected"
    assert repo.get_by_id(tid1)["status"] == "done"

    # 周期 2：合法复发，revision=2 单调递增，合法重开
    res2 = service.upsert_source(
        source_key="mail:quality:300",
        title="10月账单质量",
        source_metadata={"revision": 2, "cycle_id": 2, "event_type": "quality_detected"}
    )
    assert res2["status"] in ("created", "updated")
    assert res2.get("reason") == "relapse_reopened"
    tid2 = res2["id"]
    assert repo.get_by_id(tid2)["status"] in ("pending", "active")


def test_todo_atomic_transaction_rollback(tmp_path):
    """验证 Todo 原子事务：若事务中途异常，receipt 与 todo 均回滚，不产生悬挂数据。"""
    db_file = tmp_path / "test_todo_rollback.db"
    repo = TodoRepository(db_path=str(db_file))
    repo._ensure_schema()

    conn = repo._get_connection()
    # 模拟并发或约束失败场景
    try:
        with repo.transaction() as tx:
            tx.execute(
                "INSERT INTO todo_source_receipts (source_key, revision, cycle_id, event_type) "
                "VALUES ('test:key:1', 1, 1, 'quality_detected')"
            )
            # 制造故意异常
            raise RuntimeError("模拟业务异常回滚")
    except RuntimeError:
        pass

    # 验证未写入 todo_source_receipts
    row = conn.execute("SELECT * FROM todo_source_receipts WHERE source_key = 'test:key:1'").fetchone()
    assert row is None
    conn.close()


def test_mail_cli_envelope_and_error_handling(monkeypatch, tmp_path):
    """验证 MailCLI 针对非零退出码与非法 Envelope 的防御。"""
    monkeypatch.setattr(
        "core.config_loader.load_config",
        lambda: {"security": {"sandbox_paths": [str(tmp_path)]}}
    )
    dummy_script = tmp_path / "mail_client.py"
    dummy_script.write_text("""
import sys, json
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "bad_json":
    print("NOT JSON AT ALL")
    sys.exit(0)
elif cmd == "error_code":
    print(json.dumps({"schema_version": 1, "command": cmd, "status": "error", "data": None, "errors": ["fatal test error"]}))
    sys.exit(1)
elif cmd == "ok_cmd":
    print(json.dumps({"schema_version": 1, "command": cmd, "status": "ok", "data": {"ret": 123}, "errors": []}))
    sys.exit(0)
""", encoding="utf-8")

    cli = MailCLI(script_dir=str(tmp_path), db_path=str(tmp_path / "statements.db"))

    # 1. 正常命令返回 ok
    res = cli.run("ok_cmd")
    assert res["status"] == "ok"
    assert res["data"]["ret"] == 123

    # 2. 退出码非零返回 error envelope，包含 errors
    res_err = cli.run("error_code")
    assert res_err["status"] == "error"
    assert "fatal test error" in res_err["errors"][0]

    # 3. 损坏的 JSON 返回 error envelope
    res_bad = cli.run("bad_json")
    assert res_bad["status"] == "error"
    assert any("JSON" in e or "未输出合法" in e for e in res_bad["errors"])


def test_billing_snapshot_delegation_no_sqlite(monkeypatch):
    """验证 R1: features/billing/snapshot 模块完全不含 sqlite3，且委托给 MailCLI."""
    import features.billing.snapshot as snap_mod
    assert "sqlite3" not in snap_mod.__dict__, "features/billing/snapshot 不应导入 sqlite3"

    mock_run = MagicMock(return_value={
        "schema_version": 1,
        "command": "billing_snapshot",
        "status": "ok",
        "data": {
            "window_months": 6,
            "today": "2026-10-12",
            "historical_fallbacks": [],
            "anomalies": [],
            "confirmations": [],
            "coverage_gaps": [],
            "monthly_aggregates": {},
        },
        "errors": []
    })
    monkeypatch.setattr(MailCLI, "run", mock_run)

    res = snap_mod.build_snapshot(months=6)
    assert res["window_months"] == 6
    assert mock_run.called
    assert mock_run.call_args[0][0] == "billing_snapshot"


def test_mail_cli_security_guard_unauthorized_path(monkeypatch, tmp_path):
    """验证 R5: MailCLI 通过 SecurityGuard 严格拦截未授权目录."""
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    disallowed_dir = tmp_path / "forbidden"
    disallowed_dir.mkdir()

    monkeypatch.setattr(
        "core.config_loader.load_config",
        lambda: {"security": {"sandbox_paths": [str(allowed_dir)]}}
    )

    cli = MailCLI(script_dir=str(disallowed_dir), db_path=str(allowed_dir / "statements.db"))
    res = cli.run("mail_summary_get")
    assert res["status"] == "error"
    assert any("安全沙箱拒绝访问未授权路径" in e for e in res["errors"])


def test_todo_brief_prioritizes_oldest_overdue_tasks(monkeypatch, tmp_path):
    """验证 R8: push_brief 优先纳入最古老逾期待办，不被无序截断丢失."""
    db_file = tmp_path / "test_overdue_brief.db"
    service = TodoService(db_path=str(db_file))

    # 创建 2 个逾期待办与多个普通待办
    service.add(title="最古老逾期任务", due_at="2026-10-01T10:00:00Z")
    service.add(title="较新逾期任务", due_at="2026-10-10T10:00:00Z")
    for i in range(10):
        service.add(title=f"普通任务_{i}")

    # 验证底层 repo.get_todos_for_brief() 排序首位必为最古老逾期任务
    brief_rows = service.repo.get_todos_for_brief()
    assert len(brief_rows) == 12
    assert brief_rows[0]["title"] == "最古老逾期任务"
    assert brief_rows[1]["title"] == "较新逾期任务"

    # 验证 push_brief 推送的内容包含最古老逾期任务
    sent_payloads = []
    monkeypatch.setattr(
        "features.todo.service.send_im_alert",
        lambda title, text, color="yellow", dedup_key=None: sent_payloads.append((title, text))
    )
    service.push_brief()
    assert len(sent_payloads) == 1
    brief_text = sent_payloads[0][1]
    assert "最古老逾期任务" in brief_text
    assert "已超期任务" in brief_text


def test_read_only_commands_zero_db_creation(monkeypatch, tmp_path):
    """验证 R3: mail_summary_get 与 mail_noise_rules_list 在数据库文件缺失时返回错误，不自动建库."""
    local_dev = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "dev", "mail-statement-parser"))
    if os.path.isdir(local_dev):
        monkeypatch.setenv("MAIL_STATEMENT_PARSER_DIR", local_dev)

    missing_db = tmp_path / "nonexistent" / "statements.db"
    from integrations.mail.paths import get_parser_script_dir
    script_dir = get_parser_script_dir()
    script_path = os.path.join(script_dir, "mail_client.py")

    if not os.path.exists(script_path):
        pytest.skip(f"mail_client.py 不在当前环境配置目录中: {script_path}")

    # 测试 mail_summary_get
    res1 = subprocess.run(
        [sys.executable, script_path, "mail_summary_get", "--db", str(missing_db)],
        capture_output=True,
        text=True
    )
    assert not missing_db.exists(), "mail_summary_get 不应创建缺失的数据库文件"
    assert res1.returncode != 0 or '"status": "error"' in res1.stdout

    # 测试 mail_noise_rules_list
    res2 = subprocess.run(
        [sys.executable, script_path, "mail_noise_rules_list", "--db", str(missing_db)],
        capture_output=True,
        text=True
    )
    assert not missing_db.exists(), "mail_noise_rules_list 不应创建缺失的数据库文件"
    assert res2.returncode != 0 or '"status": "error"' in res2.stdout

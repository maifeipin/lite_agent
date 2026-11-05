"""阶段 2 行为基线测试：双库路径隔离、ACK 与 Revision 严格时序、失败重试与超时语义
(dev/tests/integration/test_mail_di_baseline.py)

锁定：
1. 双库路径分离：
   - statement_db_path 严格用于 Parser 拉取、质量事件读取和 ACK，不能被待办库路径替代；
   - todo_db_path 严格用于质量待办写入、解决与还款待办核对；
   - 顶层 sync_and_consume 与独立 drain_quality_events 的路径缺省解析优先级一致；
   - 注入 MailCLI 与显式 statement_db_path 冲突时在拉取前严格拒绝。
2. ACK 与 Revision 严格语义及多轮重试：
   - 必须在 Todo 写入/解决成功后才执行 ACK；
   - ACK 必须携带 event_id, revision, todo_id；
   - 写入失败绝不触发 ACK；
   - 第一轮 ACK 失败后，下一轮重试再次消费同一事件时，必须保持幂等（不重复建 Todo）并携带正确 revision 完成 ACK；
   - 覆盖 quality_detected 与 quality_resolved 两种事件类型的重试幂等。
3. 自定义 consume_fn / drain_fn 参数兼容：
   - 自定义 consume_fn 仅接收约定标准参数，不被无条件注入未知关键字 (如 time_fn)；
   - drain_fn 位置参数与关键字参数契约严格保持。
4. 协作式 30s 消费预算与单次 fetch：
   - 单次 fetch 仅执行 1 次，拉取失败仍可消费已有事件；
   - 预算耗尽后未 ACK 事件保留待下轮重试。
"""

import os
import sqlite3
import pytest
from unittest.mock import Mock

from lite_agent.features.todo.repository import DEFAULT_DB_PATH, _ensure_schema
from lite_agent.features.todo.service import TodoService
from lite_agent.integrations.mail.client import MailCLI
import lite_agent.integrations.mail.quality_todo as compat_quality
import lite_agent.integrations.mail.sync_service as compat_sync
import lite_agent.features.billing.quality_todo as canonical_quality
import lite_agent.features.billing.sync_service as canonical_sync


def _ensure_todo_schema(db_path: str):
    _ensure_schema(db_path)
    return db_path


def _make_quality_event(event_id: str, case_key: str, event_type: str = "quality_detected", revision: int = 1, cycle_id: int = 1):
    return {
        "event_id": event_id,
        "case_key": case_key,
        "event_type": event_type,
        "bank_code": "CMB",
        "account_name": "test_account",
        "template_version": "v1.0",
        "revision": revision,
        "cycle_id": cycle_id,
        "occurrence_count": 1,
        "diagnostics": [{"code": "FORMAT_CHANGED", "severity": "warning", "field": "due_date", "reason": "pattern shift"}],
        "evidence_refs": ["msg_001"],
        "first_seen_at": "2026-11-01T00:00:00Z",
        "last_seen_at": "2026-11-05T00:00:00Z"
    }


# ==============================================================================
# 1. 数据库路径分离与优先级基线
# ==============================================================================

@pytest.mark.parametrize("consume_fn", [
    compat_quality.consume_quality_events,
    canonical_quality.consume_quality_events,
])
def test_baseline_db_paths_strictly_separated(tmp_path, consume_fn):
    """基线 1.1: statement_db_path 与 todo_db_path 严格隔离，互不串用。"""
    stmt_db = str(tmp_path / "statement.db")
    todo_db = _ensure_todo_schema(str(tmp_path / "todos.db"))

    client = Mock(spec=MailCLI)
    client.db_path = os.path.abspath(stmt_db)
    client.quality_events.return_value = [
        _make_quality_event("evt_sep", "k_sep", revision=1, cycle_id=1)
    ]
    client.quality_ack.return_value = {"acked": True}

    stats = consume_fn(client=client, db_path=todo_db)
    assert stats["created"] == 1
    assert stats["acked"] == 1

    # todo_db 中存在写入记录
    with sqlite3.connect(todo_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM todos WHERE source_key='mail_quality:k_sep'").fetchone()[0]
        assert count == 1

    # statement_db 绝不会被 TodoService 当成 todo_db 写入 todos 表
    assert not os.path.exists(stmt_db) or not sqlite3.connect(stmt_db).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='todos'"
    ).fetchone()


@pytest.mark.parametrize("sync_fn", [
    compat_sync.sync_and_consume,
    canonical_sync.sync_and_consume,
])
def test_baseline_mail_cli_db_mismatch_strictly_rejected(tmp_path, sync_fn):
    """基线 1.2: 注入的 MailCLI 与显式传入的 statement_db_path 不一致时必须拒绝拉取。"""
    cli = MailCLI(script_dir=str(tmp_path), db_path=str(tmp_path / "actual.db"))
    with pytest.raises(ValueError, match="Injected MailCLI does not use the requested statement database"):
        sync_fn(
            statement_db_path=str(tmp_path / "expected_different.db"),
            client=cli,
            run_fetch=True
        )


@pytest.mark.parametrize("sync_fn", [
    compat_sync.sync_and_consume,
    canonical_sync.sync_and_consume,
])
def test_baseline_sync_and_consume_todo_path_priority(tmp_path, monkeypatch, sync_fn):
    """基线 1.3: 顶层 sync_and_consume 遵循 显式入参 > billing配置 > DEFAULT_DB_PATH 优先级。"""
    cfg_todo = str(tmp_path / "cfg_todo.db")
    explicit_todo = str(tmp_path / "explicit_todo.db")
    monkeypatch.setattr(
        "lite_agent.core.config_loader.load_config",
        lambda: {"billing": {"todo_db_path": cfg_todo, "repayment_todo": {"enabled": False}}}
    )

    client = Mock(spec=MailCLI)
    client.quality_events.return_value = []

    captured_paths = []

    def mock_drain(cli, todo_path, *args, **kwargs):
        captured_paths.append(todo_path)
        return {"complete": True, "acked": 0, "created": 0, "errors": [], "backlog_hit": False}

    # 1. 显式传入优先于配置
    sync_fn(todo_db_path=explicit_todo, client=client, run_fetch=False, drain_fn=mock_drain)
    assert captured_paths[-1] == os.path.abspath(explicit_todo)

    # 2. 缺省显式入参时使用配置
    sync_fn(todo_db_path=None, client=client, run_fetch=False, drain_fn=mock_drain)
    assert captured_paths[-1] == os.path.abspath(cfg_todo)

    # 3. 配置也为空时使用 DEFAULT_DB_PATH
    monkeypatch.setattr("lite_agent.core.config_loader.load_config", lambda: {})
    sync_fn(todo_db_path=None, client=client, run_fetch=False, drain_fn=mock_drain)
    assert captured_paths[-1] == os.path.abspath(DEFAULT_DB_PATH)


def test_baseline_drain_quality_events_todo_path_resolution(tmp_path, monkeypatch):
    """基线 1.4: drain_quality_events 遵循 显式入参 > billing.todo_db_path 配置。"""
    cfg_todo = str(tmp_path / "cfg_drain_todo.db")
    explicit_todo = str(tmp_path / "explicit_drain_todo.db")
    monkeypatch.setattr(
        "lite_agent.core.config_loader.load_config",
        lambda: {"billing": {"todo_db_path": cfg_todo}}
    )

    client = Mock(spec=MailCLI)
    captured = []

    def mock_consume(client=None, db_path=None, limit=100, stop_on_error=False, deadline=None, **kwargs):
        captured.append(db_path)
        return {"total": 0, "errors": [], "complete": True}

    # 1. 显式路径优先
    compat_sync.drain_quality_events(client=client, todo_db_path=explicit_todo, consume_fn=mock_consume)
    assert captured[-1] == explicit_todo

    # 2. 缺省显式路径时使用配置
    compat_sync.drain_quality_events(client=client, todo_db_path=None, consume_fn=mock_consume)
    assert captured[-1] == cfg_todo



# ==============================================================================
# 2. ACK 严格语义与多轮重试幂等基线
# ==============================================================================

@pytest.mark.parametrize("consume_fn", [
    compat_quality.consume_quality_events,
    canonical_quality.consume_quality_events,
])
def test_baseline_write_failure_never_acknowledges(tmp_path, consume_fn):
    """基线 2.1: Todo 写入抛出异常时，严禁执行 ACK，事件保留未确认。"""
    todo_db = _ensure_todo_schema(str(tmp_path / "todos.db"))
    client = Mock(spec=MailCLI)
    client.quality_events.return_value = [
        _make_quality_event("evt_err", "k_err", revision=1, cycle_id=1)
    ]

    mock_upsert = Mock(side_effect=OSError("disk read-only"))
    stats = consume_fn(client=client, db_path=todo_db, upsert_fn=mock_upsert)

    assert stats["failed"] == 1
    assert stats["acked"] == 0
    client.quality_ack.assert_not_called()


@pytest.mark.parametrize("consume_fn", [
    compat_quality.consume_quality_events,
    canonical_quality.consume_quality_events,
])
def test_baseline_ack_failure_and_subsequent_retry_quality_detected(tmp_path, consume_fn):
    """基线 2.2: quality_detected 首次写入成功但 ACK 失败后，下一轮重试不重复建 Todo 并正确 ACK。"""
    todo_db = _ensure_todo_schema(str(tmp_path / "todos.db"))
    client = Mock(spec=MailCLI)
    evt = _make_quality_event("evt_detect_retry", "k_detect_retry", revision=1, cycle_id=1)

    # 第一轮: 拉取到事件，Todo 写入成功，但 ACK 拒绝或网络异常
    client.quality_events.return_value = [evt]
    client.quality_ack.return_value = {"acked": False, "reason": "ack timeout"}

    stats1 = consume_fn(client=client, db_path=todo_db)
    assert stats1["created"] == 1
    assert stats1["acked"] == 0
    assert stats1["failed_acks"] == 1
    assert client.quality_ack.call_count == 1
    ack_call_1 = client.quality_ack.call_args.kwargs
    assert ack_call_1["event_id"] == "evt_detect_retry"
    assert ack_call_1["revision"] == 1

    # 检查数据库：已有 1 条待办
    with sqlite3.connect(todo_db) as conn:
        rows = conn.execute("SELECT id, status, source_key FROM todos WHERE source_key='mail_quality:k_detect_retry'").fetchall()
        assert len(rows) == 1
        initial_todo_id = rows[0][0]
        assert rows[0][1] == "pending"

    # 第二轮重试: 上游因未收到 ACK，再次派发同一事件
    client.quality_events.return_value = [evt]
    client.quality_ack.reset_mock()
    client.quality_ack.return_value = {"acked": True}

    stats2 = consume_fn(client=client, db_path=todo_db)
    assert stats2["acked"] == 1
    assert stats2["failed"] == 0
    assert stats2["failed_acks"] == 0
    assert client.quality_ack.call_count == 1
    ack_call_2 = client.quality_ack.call_args.kwargs
    assert ack_call_2["event_id"] == "evt_detect_retry"
    assert ack_call_2["revision"] == 1
    assert ack_call_2["todo_id"] == initial_todo_id

    # 再次检查数据库：绝对没有重复创建，总数仍为 1 条
    with sqlite3.connect(todo_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM todos WHERE source_key='mail_quality:k_detect_retry'").fetchone()[0]
        assert count == 1


@pytest.mark.parametrize("consume_fn", [
    compat_quality.consume_quality_events,
    canonical_quality.consume_quality_events,
])
def test_baseline_ack_failure_and_subsequent_retry_quality_resolved(tmp_path, consume_fn):
    """基线 2.3: quality_resolved 解决成功但 ACK 失败后，下一轮重试保持幂等并正确完成 ACK。"""
    todo_db = _ensure_todo_schema(str(tmp_path / "todos.db"))
    client = Mock(spec=MailCLI)

    # 先创建一个初始待办
    service = TodoService(todo_db)
    init_res = service.upsert_source(
        source_key="mail_quality:k_res_retry",
        title="测试质量待办",
        created_via="mail_quality",
        source_metadata={"revision": 1, "cycle_id": 1}
    )
    todo_id = init_res["id"]

    evt_resolve = _make_quality_event("evt_res_01", "k_res_retry", event_type="quality_resolved", revision=2, cycle_id=1)

    # 第一轮: 解决待办，但 ACK 抛出异常
    client.quality_events.return_value = [evt_resolve]
    client.quality_ack.side_effect = RuntimeError("network rpc dropped")

    stats1 = consume_fn(client=client, db_path=todo_db)
    assert stats1["resolved"] == 1
    assert stats1["failed_acks"] == 1
    assert stats1["acked"] == 0

    with sqlite3.connect(todo_db) as conn:
        status = conn.execute("SELECT status FROM todos WHERE id=?", (todo_id,)).fetchone()[0]
        assert status == "done"

    # 第二轮重试: 事件再次到来，再次调用解决（已是 done 状态），此轮 ACK 成功
    client.quality_events.return_value = [evt_resolve]
    client.quality_ack.reset_mock()
    client.quality_ack.side_effect = None
    client.quality_ack.return_value = {"acked": True}

    stats2 = consume_fn(client=client, db_path=todo_db)
    assert stats2["acked"] == 1
    assert stats2["failed"] == 0
    assert stats2["failed_acks"] == 0
    ack_call = client.quality_ack.call_args.kwargs
    assert ack_call["event_id"] == "evt_res_01"
    assert ack_call["revision"] == 2
    assert ack_call["todo_id"] == todo_id

    # 数据库仍保持 1 条已解决待办
    with sqlite3.connect(todo_db) as conn:
        rows = conn.execute("SELECT id, status FROM todos WHERE source_key='mail_quality:k_res_retry'").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == todo_id
        assert rows[0][1] == "done"


# ==============================================================================
# 3. 自定义 consume_fn / drain_fn 参数契约与兼容性
# ==============================================================================

def test_baseline_custom_consume_fn_parameter_compatibility(tmp_path):
    """基线 3.1: 自定义 consume_fn 仅接收约定标准参数，不能被无条件注入额外关键字 (如 time_fn)。"""
    todo_db = str(tmp_path / "todos.db")
    client = Mock(spec=MailCLI)
    client.quality_events.return_value = []

    called_args = {}

    def custom_consume(client=None, db_path=None, limit=100, stop_on_error=False, deadline=None):
        called_args.update({
            "client": client,
            "db_path": db_path,
            "limit": limit,
            "stop_on_error": stop_on_error,
            "deadline": deadline,
        })
        return {"total": 0, "errors": [], "complete": True}

    # 正式 drain_quality_events 必须能直接调用没有 **kwargs、没有 time_fn 参数的标准消费回调
    stats = canonical_sync.drain_quality_events(client=client, todo_db_path=todo_db, consume_fn=custom_consume)
    assert stats["complete"] is True
    assert called_args["client"] is client
    assert called_args["db_path"] == todo_db
    assert called_args["limit"] == 100
    assert called_args["stop_on_error"] is True
    assert called_args["deadline"] is not None


@pytest.mark.parametrize("sync_fn", [
    compat_sync.sync_and_consume,
    canonical_sync.sync_and_consume,
])
def test_baseline_custom_drain_fn_invocation_in_sync_and_consume(tmp_path, monkeypatch, sync_fn):
    """基线 3.2: sync_and_consume 能够正确将已解析的路径与参数传递给自定义 drain_fn。"""
    monkeypatch.setattr(
        "lite_agent.core.config_loader.load_config",
        lambda: {"billing": {"repayment_todo": {"enabled": False}}}
    )
    client = Mock(spec=MailCLI)
    todo_db = str(tmp_path / "todos.db")

    drain_called_with = []

    def custom_drain(cli, todo_path, event_limit, max_batches):
        drain_called_with.append((cli, todo_path, event_limit, max_batches))
        return {
            "complete": True,
            "acked": 0,
            "created": 0,
            "errors": [],
            "backlog_hit": False,
        }

    res = sync_fn(
        todo_db_path=todo_db,
        event_limit=50,
        max_batches=2,
        client=client,
        run_fetch=False,
        drain_fn=custom_drain
    )

    assert res["status"] == "ok"
    assert len(drain_called_with) == 1
    cli, path, limit, batches = drain_called_with[0]
    assert cli is client
    assert path == os.path.abspath(todo_db)
    assert limit == 50
    assert batches == 2


# ==============================================================================
# 4. 单次 Fetch、协作式 30s 预算与超时语义基线
# ==============================================================================

@pytest.mark.parametrize("sync_fn", [
    compat_sync.sync_and_consume,
    canonical_sync.sync_and_consume,
])
def test_baseline_sync_fetch_single_execution_on_failure(tmp_path, monkeypatch, sync_fn):
    """基线 4.1: sync_fetch 遇到失败时仅执行 1 次，绝不重复拉取，但继续消费已有事件。"""
    monkeypatch.setattr(
        "lite_agent.core.config_loader.load_config",
        lambda: {"billing": {"repayment_todo": {"enabled": False}}}
    )
    client = Mock(spec=MailCLI)
    client.sync_fetch.side_effect = RuntimeError("parser network timeout")
    client.quality_events.return_value = []

    mock_drain = Mock(return_value={
        "complete": True,
        "acked": 0,
        "created": 0,
        "errors": [],
        "backlog_hit": False
    })

    res = sync_fn(client=client, run_fetch=True, drain_fn=mock_drain)

    assert client.sync_fetch.call_count == 1
    assert mock_drain.call_count == 1
    assert res["fetch_success"] is False
    assert res["quality_success"] is True
    assert res["status"] == "failed"


@pytest.mark.parametrize("consume_fn", [
    compat_quality.consume_quality_events,
    canonical_quality.consume_quality_events,
])
def test_baseline_budget_expiry_keeps_event_unacked(tmp_path, consume_fn):
    """基线 4.2: 待办消费过程中若协作式预算耗尽，未 ACK 事件保留待重试。"""
    client = Mock(spec=MailCLI)
    client.quality_events.return_value = [
        _make_quality_event("evt_timeout", "k_timeout", revision=1, cycle_id=1)
    ]

    # deadline 设定为过去时间戳
    stats = consume_fn(client=client, db_path=str(tmp_path / "todos.db"), deadline=1.0, time_fn=Mock(monotonic=lambda: 2.0))
    assert stats["failed"] == 1
    assert stats["acked"] == 0
    assert any("预算耗尽" in err for err in stats["errors"])
    client.quality_ack.assert_not_called()


# ==============================================================================
# 5. 阶段 2 提交 2 专项验收：路径优先级、时钟统一传递、写入后超时与 is None 注入
# ==============================================================================

def test_canonical_drain_quality_events_todo_path_resolution_priorities(tmp_path, monkeypatch):
    """验收 5.1: 正式 drain_quality_events 覆盖 显式路径 > billing 配置 > Repository 默认路径，且不读取 ops_todo。"""
    cfg_todo = str(tmp_path / "cfg_drain.db")
    explicit_todo = str(tmp_path / "explicit_drain.db")
    monkeypatch.setattr(
        "lite_agent.core.config_loader.load_config",
        lambda: {"billing": {"todo_db_path": cfg_todo}}
    )

    client = Mock(spec=MailCLI)
    captured = []

    def mock_consume(client=None, db_path=None, limit=100, stop_on_error=False, deadline=None, **kwargs):
        captured.append(db_path)
        return {"total": 0, "errors": [], "complete": True}

    # 1. 显式路径优先，且不被覆盖
    canonical_sync.drain_quality_events(client=client, todo_db_path=explicit_todo, consume_fn=mock_consume)
    assert captured[-1] == explicit_todo

    # 2. 缺省显式路径时使用 billing 配置
    canonical_sync.drain_quality_events(client=client, todo_db_path=None, consume_fn=mock_consume)
    assert captured[-1] == cfg_todo

    # 3. billing 配置也为空时，兜底 DEFAULT_DB_PATH
    monkeypatch.setattr("lite_agent.core.config_loader.load_config", lambda: {})
    canonical_sync.drain_quality_events(client=client, todo_db_path=None, consume_fn=mock_consume)
    assert captured[-1] == DEFAULT_DB_PATH

    # 4. 验证 features/billing/sync_service 源码中绝不包含 ops_todo 导入
    import inspect
    src = inspect.getsource(canonical_sync)
    assert "ops_todo" not in src


def test_canonical_drain_clock_propagation_and_custom_consume_compatibility(tmp_path):
    """验收 5.2: 默认消费、deadline 与积压探测统一使用传入的 time_fn；无 time_fn 参数的自定义消费回调仍能顺利执行。"""
    # 1. 验证默认消费与探测使用统一时钟
    client = Mock(spec=MailCLI)
    client.quality_events.return_value = []
    mock_clock = Mock()
    # 模拟 monotonic 递增: deadline 计算时取 100.0 (deadline=130.0), 后续检查积压探测取 110.0 (budget=20.0)
    mock_clock.monotonic.side_effect = [100.0, 110.0, 110.0]

    stats = canonical_sync.drain_quality_events(
        client=client,
        todo_db_path=str(tmp_path / "todo.db"),
        time_fn=mock_clock
    )
    assert stats["complete"] is True
    assert mock_clock.monotonic.call_count >= 1

    # 2. 验证无 time_fn 参数的自定义回调不会因注入 time_fn 抛出 TypeError
    def no_time_fn_consume(client=None, db_path=None, limit=100, stop_on_error=False, deadline=None):
        return {"total": 0, "errors": [], "complete": True}

    stats_custom = canonical_sync.drain_quality_events(
        client=client,
        todo_db_path=str(tmp_path / "todo.db"),
        consume_fn=no_time_fn_consume,
        time_fn=mock_clock
    )
    assert stats_custom["complete"] is True


def test_canonical_timeout_after_todo_write_keeps_event_unacked(tmp_path):
    """验收 5.3: 待办写入已入库成功，但在执行 ACK 前单调时钟到达 deadline，严禁执行 ACK，事件保留未确认。"""
    todo_db = _ensure_todo_schema(str(tmp_path / "todos.db"))
    client = Mock(spec=MailCLI)
    evt = _make_quality_event("evt_post_write_to", "k_post_write_to", revision=1, cycle_id=1)
    client.quality_events.return_value = [evt]

    # 控制单调时钟：
    # 1. remaining() 获取初始剩余时间拉取事件 -> 0.0 (deadline=10.0, 预算充足)
    # 2. 循环头检查超时 -> 0.0 (预算充足，继续执行 Todo 写入)
    # 3. Todo 写入成功后，准备执行 ACK 时调用 remaining() -> 11.0 (超过 deadline=10.0，预算耗尽抛出 TimeoutError)
    # 4. 下一次循环或结束检查 -> 12.0
    mock_time = Mock()
    mock_time.monotonic.side_effect = [0.0, 0.0, 11.0, 12.0]

    stats = canonical_quality.consume_quality_events(
        client=client,
        db_path=todo_db,
        deadline=10.0,
        time_fn=mock_time
    )

    # 验证待办已被写入
    assert stats["created"] == 1
    # 验证 ACK 失败且质量 ACK 绝未被调用
    assert stats["failed_acks"] == 1
    assert stats["acked"] == 0
    client.quality_ack.assert_not_called()
    assert any("预算耗尽" in err for err in stats["errors"])

    # 查库验证待办真实存在
    with sqlite3.connect(todo_db) as conn:
        row = conn.execute("SELECT id, status FROM todos WHERE source_key='mail_quality:k_post_write_to'").fetchone()
        assert row is not None
        assert row[1] == "pending"


class FalsyCallable:
    """布尔值为 False 的可调用对象，用于检验 is None 注入契约，防止因布尔假被错误替换为默认实现。"""
    def __init__(self, return_val=None):
        self.called = False
        self.call_args = None
        self.return_val = return_val

    def __bool__(self):
        return False

    def __call__(self, *args, **kwargs):
        self.called = True
        self.call_args = (args, kwargs)
        if self.return_val is not None:
            return self.return_val
        return {"id": "falsy_todo_id", "status": "created", "created": True}


def test_canonical_di_accepts_falsy_callable(tmp_path):
    """验收 5.4: 验证布尔值为 False 的合法可调用对象仍被优先使用，绝不回退到默认实现。"""
    falsy_upsert = FalsyCallable()
    assert not bool(falsy_upsert)  # 确认其布尔值为 False

    client = Mock(spec=MailCLI)
    client.quality_events.return_value = [
        _make_quality_event("evt_falsy", "k_falsy", revision=1, cycle_id=1)
    ]
    client.quality_ack.return_value = {"acked": True}

    stats = canonical_quality.consume_quality_events(
        client=client,
        db_path=str(tmp_path / "todos.db"),
        upsert_fn=falsy_upsert
    )

    assert falsy_upsert.called is True
    assert stats["created"] == 1
    assert stats["acked"] == 1
    # ACK 时携带的 todo_id 来自 falsy_upsert 返回的 "falsy_todo_id"
    assert client.quality_ack.call_args.kwargs["todo_id"] == "falsy_todo_id"

    # 同样测试 falsy consume_fn 与 falsy drain_fn
    falsy_consume = FalsyCallable(return_val={"total": 0, "errors": [], "complete": True})
    stats_drain = canonical_sync.drain_quality_events(
        client=client,
        todo_db_path=str(tmp_path / "todos.db"),
        consume_fn=falsy_consume
    )
    assert falsy_consume.called is True
    assert stats_drain["complete"] is True


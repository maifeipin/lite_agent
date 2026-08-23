"""
Phase 3a — ExecutionLedger 验收测试

覆盖 11 项验收标准:
  1.  每次 Agent/Worker Runtime 都有唯一 execution_id
  2.  事件顺序可完整还原
  3.  Worker 记录正确的 parent_execution_id
  4.  Tool Call 与 Tool Result 通过 tool_call_id 对应
  5.  ERROR 后的 DONE 不会覆盖失败状态
  6.  重复记录同一 seq 不会产生重复行
  7.  参数和结果中的凭据不会明文入库
  8.  Ledger 写入失败不影响 Agent 返回结果
  9.  进程遗留的 running 可识别为 abandoned
  10. 并发 Worker 写入不会出现 SQLite lock 回归
  11. 现有 244 项测试继续通过 (由全套 pytest 验证)
"""

import os
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.agent_runtime import RuntimeEvent, RuntimeEventType
from core.execution import ExecutionContext, ActorType, ExecutionSource
from core.execution_ledger import ExecutionLedger, ExecutionHandle
from core.runtime_recorder import RuntimeRecorder


# ============================================================
#  Fixtures
# ============================================================

@pytest.fixture
def tmp_db():
    """临时数据库文件，每个测试独立。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    os.unlink(db_path)  # 让 ledger 自己创建
    yield db_path
    if os.path.exists(db_path):
        os.unlink(db_path)
    # 清理 WAL/SHM 文件
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.unlink(p)


@pytest.fixture
def ledger(tmp_db):
    return ExecutionLedger(db_path=tmp_db)


@pytest.fixture
def ctx():
    return ExecutionContext(
        actor_id="test_user",
        actor_type=ActorType.USER,
        source=ExecutionSource.STREAM,
        session_key="sess_test",
        max_steps=8,
        max_output_tokens=2048,
    )


def _make_event(t: RuntimeEventType, data: dict = None) -> RuntimeEvent:
    return RuntimeEvent(type=t, data=data or {})


# ============================================================
#  1. 每次 Runtime 执行都有唯一 execution_id
# ============================================================

def test_unique_execution_id_per_run(ledger, ctx):
    """两次独立的 ledger.start() 应生成不同的 execution_id。"""
    e1 = ledger.start(ctx, model_name="m1")
    e2 = ledger.start(ctx, model_name="m2")
    assert e1.id != e2.id
    assert len(e1.id) == 32  # UUID4 hex
    assert len(e2.id) == 32


def test_start_records_execution_row(ledger, ctx):
    """start() 后 executions 表应有对应行，status=running。"""
    execution = ledger.start(ctx, model_name="gpt-4", provider="openai")
    row = ledger.get_execution(execution.id)
    assert row is not None
    assert row["execution_id"] == execution.id
    assert row["status"] == "running"
    assert row["model_name"] == "gpt-4"
    assert row["provider"] == "openai"
    assert row["actor_id"] == "test_user"
    assert row["actor_type"] == "user"
    assert row["source"] == "stream"
    assert row["steps_used"] == 0
    assert row["prompt_tokens"] == 0


# ============================================================
#  2. 事件顺序可完整还原
# ============================================================

def test_event_order_preserved(ledger, ctx):
    """runtime_events 表按 seq 升序，可完整还原事件流。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    events = [
        _make_event(RuntimeEventType.STEP_START, {"step": 1, "max_steps": 8}),
        _make_event(RuntimeEventType.TEXT, "Hello"),
        _make_event(RuntimeEventType.USAGE,
                    {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
        _make_event(RuntimeEventType.DONE, {"finish_reason": "stop"}),
    ]
    for e in events:
        recorder.record(e)

    stored = ledger.list_events(execution.id)
    assert len(stored) == 4
    assert [r["seq"] for r in stored] == [0, 1, 2, 3]
    assert [r["event_type"] for r in stored] == [
        "STEP_START", "TEXT", "USAGE", "DONE"]


def test_step_usage_updated_on_events(ledger, ctx):
    """STEP_START 更新 steps_used，USAGE 累加 token。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    recorder.record(_make_event(RuntimeEventType.STEP_START, {"step": 1}))
    recorder.record(_make_event(RuntimeEventType.USAGE,
                                {"prompt_tokens": 10, "completion_tokens": 5,
                                 "total_tokens": 15}))
    recorder.record(_make_event(RuntimeEventType.STEP_START, {"step": 2}))
    recorder.record(_make_event(RuntimeEventType.USAGE,
                                {"prompt_tokens": 20, "completion_tokens": 8,
                                 "total_tokens": 28}))
    recorder.record(_make_event(RuntimeEventType.DONE, {"finish_reason": "stop"}))

    row = ledger.get_execution(execution.id)
    assert row["steps_used"] == 2
    assert row["prompt_tokens"] == 30
    assert row["completion_tokens"] == 13
    assert row["total_tokens"] == 43
    assert row["status"] == "succeeded"


# ============================================================
#  3. Worker 记录正确的 parent_execution_id
# ============================================================

def test_parent_execution_id_recorded(ledger, ctx):
    """parent_execution_id 应被正确持久化。"""
    # 模拟 Orchestrator 父执行
    parent = ledger.start(ctx, model_name="orchestrator")
    # 模拟 Worker 子执行
    worker_ctx = ExecutionContext(
        actor_id="worker_1",
        actor_type=ActorType.WORKER,
        source=ExecutionSource.ORCHESTRATOR,
        session_key="worker_worker_1",
    )
    worker_exec = ledger.start(
        worker_ctx, model_name="worker_model",
        parent_execution_id=parent.id,
    )

    row = ledger.get_execution(worker_exec.id)
    assert row["parent_execution_id"] == parent.id
    assert row["actor_type"] == "worker"
    assert row["source"] == "orchestrator"


# ============================================================
#  4. Tool Call 与 Tool Result 通过 tool_call_id 对应
# ============================================================

def test_tool_call_id_correlation(ledger, ctx):
    """TOOL_CALL 和 TOOL_RESULT 事件应携带相同的 tool_call_id。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    call_id = "call_abc123"
    recorder.record(_make_event(RuntimeEventType.TOOL_CALL, {
        "id": call_id, "name": "search", "arguments": '{"q":"test"}',
    }))
    recorder.record(_make_event(RuntimeEventType.TOOL_RESULT, {
        "id": call_id, "name": "search", "output": "result text",
        "ok": True, "duration_ms": 100,
    }))

    events = ledger.list_events(execution.id)
    assert len(events) == 2
    # 两个事件都应携带 tool_call_id
    assert events[0]["tool_call_id"] == call_id
    assert events[1]["tool_call_id"] == call_id
    assert events[0]["tool_name"] == "search"
    assert events[1]["tool_name"] == "search"


# ============================================================
#  5. ERROR 后的 DONE 不会覆盖失败状态
# ============================================================

def test_done_does_not_overwrite_error(ledger, ctx):
    """ERROR 事件后到达的 DONE 不能把 status 从 failed 改回 succeeded。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    recorder.record(_make_event(RuntimeEventType.STEP_START, {"step": 1}))
    recorder.record(_make_event(RuntimeEventType.ERROR, {"msg": "model timeout"}))
    # 模拟 Runtime 在 ERROR 后又发了 DONE (不应覆盖失败)
    recorder.record(_make_event(RuntimeEventType.DONE, {"finish_reason": "stop"}))

    row = ledger.get_execution(execution.id)
    assert row["status"] == "failed"
    assert "model timeout" in (row["terminal_reason"] or "")


def test_done_does_not_overwrite_dead_loop(ledger, ctx):
    """DEAD_LOOP 后到达的 DONE 也不能覆盖。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    recorder.record(_make_event(RuntimeEventType.DEAD_LOOP,
                                {"msg": "loop detected", "tool_name": "search"}))
    recorder.record(_make_event(RuntimeEventType.DONE, {"finish_reason": "stop"}))

    row = ledger.get_execution(execution.id)
    assert row["status"] == "dead_loop"


def test_done_does_not_overwrite_max_steps(ledger, ctx):
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)
    recorder.record(_make_event(RuntimeEventType.MAX_STEPS, {"max_steps": 8}))
    recorder.record(_make_event(RuntimeEventType.DONE, {"finish_reason": "stop"}))
    row = ledger.get_execution(execution.id)
    assert row["status"] == "max_steps"


def test_done_does_not_overwrite_token_budget(ledger, ctx):
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)
    recorder.record(_make_event(RuntimeEventType.TOKEN_BUDGET_EXCEEDED,
                                {"budget": 1000, "used": 1001}))
    recorder.record(_make_event(RuntimeEventType.DONE, {"finish_reason": "stop"}))
    row = ledger.get_execution(execution.id)
    assert row["status"] == "token_budget_exceeded"


# ============================================================
#  6. 重复记录同一 seq 不会产生重复行
# ============================================================

def test_duplicate_seq_ignored(ledger, ctx):
    """相同 (execution_id, seq) 的重复写入应被 INSERT OR IGNORE 忽略。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    # 模拟消费端重试导致同一事件被记录两次
    event = _make_event(RuntimeEventType.TEXT, "Hello")
    recorder.record(event)
    # 强制重置 seq 重发 (模拟 bug)
    recorder._seq = 0
    recorder.record(event)
    recorder.flush()  # TEXT 进入批量缓冲，需 flush 才入库

    events = ledger.list_events(execution.id)
    assert len(events) == 1  # 不会产生重复行


# ============================================================
#  7. 参数和结果中的凭据不会明文入库
# ============================================================

def test_secrets_in_tool_args_masked(ledger, ctx):
    """TOOL_CALL 的 arguments 中的密码应被脱敏。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    recorder.record(_make_event(RuntimeEventType.TOOL_CALL, {
        "id": "c1", "name": "db_query",
        "arguments": '{"password":"hunter2","query":"SELECT 1"}',
    }))

    events = ledger.list_events(execution.id)
    import json
    payload = json.loads(events[0]["payload_json"])
    args = payload["arguments_redacted"]
    # password 值应被替换为 ***
    assert "hunter2" not in args
    assert "***" in args


def test_secrets_in_tool_result_masked(ledger, ctx):
    """TOOL_RESULT 的 output 中的连接串应被脱敏。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    secret_output = 'postgresql://user:secret_pw@db.local:5432/prod'
    recorder.record(_make_event(RuntimeEventType.TOOL_RESULT, {
        "id": "c1", "name": "db_query", "output": secret_output,
        "ok": True, "duration_ms": 50,
    }))

    events = ledger.list_events(execution.id)
    import json
    payload = json.loads(events[0]["payload_json"])
    preview = payload["output_preview"]
    assert "secret_pw" not in preview
    assert "***" in preview
    # 长度和 hash 仍保留
    assert payload["output_length"] == len(secret_output)
    assert len(payload["output_hash"]) == 64  # SHA-256


def test_text_only_stores_length_hash_preview(ledger, ctx):
    """TEXT 事件只存 length + sha256 + 200 字脱敏预览，不存完整正文。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    long_text = "A" * 500 + "B" * 500
    recorder.record(_make_event(RuntimeEventType.TEXT, long_text))
    recorder.flush()  # TEXT 进入批量缓冲，需 flush 才入库

    events = ledger.list_events(execution.id)
    import json
    payload = json.loads(events[0]["payload_json"])
    # 只有这三个字段
    assert set(payload.keys()) == {"length", "sha256", "preview"}
    assert payload["length"] == 1000
    assert len(payload["sha256"]) == 64
    # 预览不超过 200 字
    assert len(payload["preview"]) <= 200


def test_reasoning_no_preview_stored(ledger, ctx):
    """REASONING 事件不保存正文，只存 length + sha256。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    recorder.record(_make_event(RuntimeEventType.REASONING, "thinking..."))
    recorder.flush()  # REASONING 进入批量缓冲，需 flush 才入库

    events = ledger.list_events(execution.id)
    import json
    payload = json.loads(events[0]["payload_json"])
    assert set(payload.keys()) == {"length", "sha256"}
    assert "preview" not in payload
    assert payload["length"] == len("thinking...")


# ============================================================
#  8. Ledger 写入失败不影响 Agent 返回结果
# ============================================================

def test_ledger_failure_does_not_block_agent(ledger, ctx):
    """数据库写入失败时，recorder.wrap() 仍应正常转发事件。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    # 模拟 ledger.record_event 抛异常 (如磁盘满)
    with patch.object(ledger, "record_event",
                      side_effect=Exception("disk full")):
        events = [
            _make_event(RuntimeEventType.STEP_START, {"step": 1}),
            _make_event(RuntimeEventType.TEXT, "Hello"),
            _make_event(RuntimeEventType.DONE, {"finish_reason": "stop"}),
        ]

    # wrap 应继续转发所有事件，不阻断
    received = list(recorder.wrap(iter(events)))
    assert len(received) == 3
    assert received[0].type == RuntimeEventType.STEP_START
    assert received[2].type == RuntimeEventType.DONE


def test_safe_write_swallows_exceptions(tmp_db):
    """_safe_write 失败时仅记日志，不抛异常。"""
    ledger = ExecutionLedger(db_path=tmp_db)
    # 用一个不存在的路径触发写入失败
    ledger.db_path = "/nonexistent/path/db.db"
    # 不应抛异常
    ledger._safe_write(
        "INSERT INTO executions (execution_id) VALUES (?)", ("test",))


# ============================================================
#  9. 进程遗留的 running 可识别为 abandoned
# ============================================================

def test_recover_abandoned_marks_old_running(ledger, ctx):
    """超过阈值的 running 记录应被标记为 abandoned。"""
    execution = ledger.start(ctx)
    # 手动把 started_at 改到 2 小时前
    old_time = time.time() - 7200
    ledger._safe_write(
        "UPDATE executions SET started_at = ? WHERE execution_id = ?",
        (old_time, execution.id),
    )

    n = ledger.recover_abandoned(threshold_seconds=3600)
    assert n == 1

    row = ledger.get_execution(execution.id)
    assert row["status"] == "abandoned"
    assert row["terminal_reason"] == "process_interrupted"
    assert row["finished_at"] is not None


def test_recover_abandoned_skips_recent_running(ledger, ctx):
    """未超过阈值的 running 不应被误判为 abandoned。"""
    execution = ledger.start(ctx)
    n = ledger.recover_abandoned(threshold_seconds=3600)
    assert n == 0
    row = ledger.get_execution(execution.id)
    assert row["status"] == "running"


def test_find_abandoned_returns_only_abandoned(ledger, ctx):
    """find_abandoned 只返回 abandoned 记录。"""
    e1 = ledger.start(ctx)
    e2 = ledger.start(ctx)
    # 把 e1 标记为 abandoned
    old_time = time.time() - 7200
    ledger._safe_write(
        "UPDATE executions SET started_at = ? WHERE execution_id = ?",
        (old_time, e1.id),
    )
    ledger.recover_abandoned(threshold_seconds=3600)

    abandoned = ledger.find_abandoned()
    assert len(abandoned) == 1
    assert abandoned[0]["execution_id"] == e1.id
    assert abandoned[0]["status"] == "abandoned"


# ============================================================
#  10. 并发 Worker 写入不会出现 SQLite lock 回归
# ============================================================

def test_concurrent_writes_no_deadlock(ledger, ctx):
    """多个线程并发写入同一 ledger 不应出现死锁或 lock 错误。"""
    n_threads = 8
    n_events_per_thread = 20
    errors = []

    def writer(thread_id: int):
        try:
            t_ctx = ExecutionContext(
                actor_id=f"worker_{thread_id}",
                actor_type=ActorType.WORKER,
                source=ExecutionSource.ORCHESTRATOR,
                session_key=f"worker_{thread_id}",
            )
            execution = ledger.start(t_ctx, model_name=f"m{thread_id}")
            recorder = RuntimeRecorder(ledger, execution.id)
            for i in range(n_events_per_thread):
                recorder.record(_make_event(
                    RuntimeEventType.TEXT, f"thread-{thread_id}-event-{i}"))
            recorder.flush()  # flush 批量缓冲
            ledger.finish(execution.id, status="succeeded")
        except Exception as e:
            errors.append((thread_id, str(e)))

    threads = [threading.Thread(target=writer, args=(i,))
               for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"并发写入出现错误: {errors}"
    # 应有 n_threads 条 execution 记录
    recent = ledger.list_recent(limit=100)
    assert len(recent) == n_threads
    # 每条都应是 succeeded
    assert all(r["status"] == "succeeded" for r in recent)
    # 每条应有 n_events_per_thread 个事件
    for r in recent:
        events = ledger.list_events(r["execution_id"])
        assert len(events) == n_events_per_thread


# ============================================================
#  附加: wrap() 生成器端到端测试
# ============================================================

def test_wrap_finishes_on_runtime_exception(ledger, ctx):
    """Runtime 异常退出时，wrap 应标记 execution 为 failed。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    def failing_runtime():
        yield _make_event(RuntimeEventType.STEP_START, {"step": 1})
        raise RuntimeError("model crashed")

    with pytest.raises(RuntimeError, match="model crashed"):
        list(recorder.wrap(failing_runtime()))

    row = ledger.get_execution(execution.id)
    assert row["status"] == "failed"
    assert "runtime_exception" in (row["terminal_reason"] or "")


def test_wrap_finishes_on_done(ledger, ctx):
    """正常 DONE 事件应将 execution 标记为 succeeded。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    def runtime():
        yield _make_event(RuntimeEventType.STEP_START, {"step": 1})
        yield _make_event(RuntimeEventType.TEXT, "Hello")
        yield _make_event(RuntimeEventType.DONE, {"finish_reason": "stop"})

    received = list(recorder.wrap(runtime()))
    assert len(received) == 3

    row = ledger.get_execution(execution.id)
    assert row["status"] == "succeeded"
    assert row["terminal_reason"] == "stop"


def test_explicit_finish_after_no_terminal_event(ledger, ctx):
    """未收到终态事件时，显式 finish 应将 status 置为 succeeded。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    recorder.record(_make_event(RuntimeEventType.STEP_START, {"step": 1}))
    recorder.record(_make_event(RuntimeEventType.TEXT, "partial"))

    # 未收到 DONE/ERROR 等
    row_before = ledger.get_execution(execution.id)
    assert row_before["status"] == "running"

    ledger.finish(execution.id, status="succeeded", terminal_reason="no_done")
    row_after = ledger.get_execution(execution.id)
    assert row_after["status"] == "succeeded"
    assert row_after["duration_ms"] is not None
    assert row_after["finished_at"] is not None


def test_explicit_finish_ignored_after_terminal(ledger, ctx):
    """已是终态时，再调 finish 不应覆盖。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    recorder.record(_make_event(RuntimeEventType.ERROR, {"msg": "fail"}))
    # 尝试用 succeeded 覆盖
    ledger.finish(execution.id, status="succeeded", terminal_reason="override")

    row = ledger.get_execution(execution.id)
    assert row["status"] == "failed"  # 仍是 failed，未被覆盖


# ============================================================
#  附加: TOOL_CALLS_READY payload
# ============================================================

def test_tool_calls_ready_payload(ledger, ctx):
    """TOOL_CALLS_READY 应记录工具声明摘要 (id + name + args_hash)。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    recorder.record(_make_event(RuntimeEventType.TOOL_CALLS_READY, {
        "content": "let me search",
        "tool_calls": [
            {"id": "c1", "function": {"name": "search",
                                       "arguments": '{"q":"test"}'}},
            {"id": "c2", "function": {"name": "calc",
                                       "arguments": '{"x":1}'}},
        ],
        "reasoning_content": "thinking",
    }))

    events = ledger.list_events(execution.id)
    import json
    payload = json.loads(events[0]["payload_json"])
    assert payload["content_length"] == len("let me search")
    assert len(payload["tool_calls_summary"]) == 2
    assert payload["tool_calls_summary"][0]["name"] == "search"
    assert len(payload["tool_calls_summary"][0]["arguments_hash"]) == 64
    # 不应保存完整 arguments
    assert "arguments" not in payload["tool_calls_summary"][0]


# ============================================================
#  P1b: 重放 USAGE/STEP_START 不重复计费 (原子投影)
# ============================================================

def test_replayed_usage_not_double_counted(ledger, ctx):
    """同一 (execution_id, seq) 的 USAGE 被重放不应重复累加 token。

    record_and_project 仅在 INSERT 真正生效时才投影概要。
    """
    execution = ledger.start(ctx)
    # 第一次写入 seq=0 USAGE
    inserted1 = ledger.record_and_project(
        execution_id=execution.id, seq=0, event_type="USAGE",
        payload={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    assert inserted1 is True
    # 重放同一 seq (模拟消费端重试)
    inserted2 = ledger.record_and_project(
        execution_id=execution.id, seq=0, event_type="USAGE",
        payload={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    assert inserted2 is False  # 重复被忽略

    row = ledger.get_execution(execution.id)
    # token 只累加一次，不会变成 30/10/30
    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 5
    assert row["total_tokens"] == 15

    events = ledger.list_events(execution.id)
    assert len(events) == 1  # 事件表也只有一行


def test_replayed_step_start_not_double_counted(ledger, ctx):
    """重放 STEP_START 不应重复累加步数 (虽 MAX 已防重复，但验证非异常)。"""
    execution = ledger.start(ctx)
    ledger.record_and_project(
        execution_id=execution.id, seq=0, event_type="STEP_START",
        payload={"step": 1},
    )
    # 重放
    ledger.record_and_project(
        execution_id=execution.id, seq=0, event_type="STEP_START",
        payload={"step": 1},
    )
    row = ledger.get_execution(execution.id)
    assert row["steps_used"] == 1  # 仍是 1，不是 2


def test_replayed_terminal_event_idempotent(ledger, ctx):
    """重放终态事件 (ERROR) 不应让状态反复变化。"""
    execution = ledger.start(ctx)
    # 第一次 ERROR: 状态变 failed
    ledger.record_and_project(
        execution_id=execution.id, seq=0, event_type="ERROR",
        payload={"msg": "boom"},
    )
    row1 = ledger.get_execution(execution.id)
    assert row1["status"] == "failed"

    # 重放同一 seq ERROR: 概要不更新 (因 INSERT 被忽略)
    ledger.record_and_project(
        execution_id=execution.id, seq=0, event_type="ERROR",
        payload={"msg": "boom"},
    )
    row2 = ledger.get_execution(execution.id)
    assert row2["status"] == "failed"
    # 终态时间不变 (未再次投影)
    assert row2["finished_at"] == row1["finished_at"]


# ============================================================
#  P1a: 1000 个 TEXT delta 写入耗时上限
# ============================================================

def test_1000_text_deltas_within_time_limit(ledger, ctx):
    """1000 个 TEXT delta 写入应在合理时间内完成 (批量聚合)。

    上限设为 5 秒 (开发机 SSD)。若逐条提交 + 写锁串行，通常远超此值。
    """
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    start = time.time()
    for i in range(1000):
        recorder.record(_make_event(RuntimeEventType.TEXT, f"delta-{i}"))
    recorder.flush()  # flush 剩余缓冲
    elapsed = time.time() - start

    # 事件全部入库
    events = ledger.list_events(execution.id)
    assert len(events) == 1000
    # seq 顺序正确
    assert events[0]["seq"] == 0
    assert events[999]["seq"] == 999

    # 耗时上限: 5 秒 (允许 CI 环境波动)
    assert elapsed < 5.0, f"1000 个 TEXT delta 写入耗时 {elapsed:.2f}s > 5s"


def test_text_deltas_batched_by_size(ledger, ctx):
    """TEXT delta 累积超过 4KB 应自动 flush。"""
    execution = ledger.start(ctx)
    recorder = RuntimeRecorder(ledger, execution.id)

    # 每条 1000 字符，4 条即超过 4KB 阈值
    big_text = "A" * 1000
    for i in range(5):
        recorder.record(_make_event(RuntimeEventType.TEXT, big_text))

    # 应已自动 flush 至少一次
    events = ledger.list_events(execution.id)
    assert len(events) == 5


# ============================================================
#  P2: abandoned duration_ms 按每行 started_at 计算
# ============================================================

def test_abandoned_duration_per_row(ledger, ctx):
    """多个 running 记录的 started_at 不同，abandoned 的 duration_ms 应不同。"""
    e1 = ledger.start(ctx)
    # e1 started 2 小时前
    ledger._safe_write(
        "UPDATE executions SET started_at = ? WHERE execution_id = ?",
        (time.time() - 7200, e1.id),
    )
    e2 = ledger.start(ctx)
    # e2 started 1 小时前
    ledger._safe_write(
        "UPDATE executions SET started_at = ? WHERE execution_id = ?",
        (time.time() - 3600, e2.id),
    )

    n = ledger.recover_abandoned(threshold_seconds=1800)  # 30 分钟阈值
    assert n == 2

    r1 = ledger.get_execution(e1.id)
    r2 = ledger.get_execution(e2.id)
    # e1 的时长应明显大于 e2
    assert r1["duration_ms"] > r2["duration_ms"]
    # e1 约 7200s, e2 约 3600s
    assert abs(r1["duration_ms"] - 7200000) < 10000  # 容差 10s
    assert abs(r2["duration_ms"] - 3600000) < 10000


# ============================================================
#  契约: ExecutionHandle.parent_execution_id 使用解析后的值
# ============================================================

def test_handle_parent_id_from_ctx(ledger):
    """当 parent_execution_id 参数为空但 ctx.parent_execution_id 有值时,
    句柄返回的 parent_execution_id 应与数据库一致 (使用解析后的值)。
    """
    ctx = ExecutionContext(
        actor_id="worker",
        actor_type=ActorType.WORKER,
        source=ExecutionSource.ORCHESTRATOR,
        session_key="sess",
        parent_execution_id="parent_from_ctx_123",
    )
    # 不显式传 parent_execution_id 参数
    handle = ledger.start(ctx, model_name="m")
    # 句柄应使用解析后的值 (来自 ctx)
    assert handle.parent_execution_id == "parent_from_ctx_123"

    row = ledger.get_execution(handle.id)
    # 数据库也应一致
    assert row["parent_execution_id"] == "parent_from_ctx_123"


def test_handle_parent_id_explicit_overrides_ctx(ledger):
    """显式参数 parent_execution_id 应覆盖 ctx.parent_execution_id。"""
    ctx = ExecutionContext(
        actor_id="worker",
        actor_type=ActorType.WORKER,
        source=ExecutionSource.ORCHESTRATOR,
        session_key="sess",
        parent_execution_id="from_ctx",
    )
    handle = ledger.start(ctx, model_name="m", parent_execution_id="explicit")
    assert handle.parent_execution_id == "explicit"
    row = ledger.get_execution(handle.id)
    assert row["parent_execution_id"] == "explicit"


# ============================================================
#  P0 端到端: Worker.run() 接入 ledger 并记录 parent_execution_id
# ============================================================

def test_worker_run_records_parent_execution_id(tmp_db, engine_with_test_skills):
    """Worker.run() 接入 ledger 后，子执行应记录 parent_execution_id。

    模拟 Orchestrator: 先 ledger.start() 建父 execution，
    再创建 WorkerAgent(ledger=...) 调 run(parent_execution_id=parent.id)。
    验证: 子执行行 parent_execution_id == 父 id，且事件已入库。
    """
    from core.worker_agent import WorkerAgent
    from core.subtask_dag import Subtask, SubtaskType
    from unittest.mock import MagicMock, patch

    ledger = ExecutionLedger(db_path=tmp_db)

    # 1) Orchestrator 父 execution
    orch_ctx = ExecutionContext(
        actor_id="orch",
        actor_type=ActorType.USER,
        source=ExecutionSource.ORCHESTRATOR,
        session_key="sess_orch",
    )
    parent = ledger.start(orch_ctx, model_name="orchestrator")

    # 2) Worker 子执行 (注入 ledger)
    worker = WorkerAgent(
        name="w1",
        client=MagicMock(),
        model_name="test-model",
        model_cfg={"max_steps": 2, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
        ledger=ledger,
    )

    subtask = Subtask(
        id="st1", name="t", type=SubtaskType.TEXT, prompt="say hi",
    )

    # 3) mock 模型返回最终回复 (一轮即 DONE)
    def mock_call_model(messages, tools, **kwargs):
        return {
            "content": "Hello!",
            "tool_calls": [],
            "finish_reason": "stop",
            "usage_total": 30,
        }

    with patch.object(worker.model_invoker, "invoke_sync",
                      side_effect=mock_call_model):
        reply, extracted = worker.run(subtask,
                                       parent_execution_id=parent.id)

    # 4) 断言子执行已记录，且 parent_execution_id 正确
    recent = ledger.list_recent(limit=10)
    # 应有 2 条: 父 + 子
    assert len(recent) == 2
    child_rows = [r for r in recent if r["parent_execution_id"] == parent.id]
    assert len(child_rows) == 1
    child = child_rows[0]
    assert child["model_name"] == "test-model"
    assert child["status"] == "succeeded"
    # 子执行应有事件入库 (STEP_START/TEXT/USAGE/DONE 至少 4 条)
    events = ledger.list_events(child["execution_id"])
    assert len(events) >= 4


# ============================================================
#  P1 父 execution 生命周期: 提前返回路径不应遗留 running
# ============================================================

def _build_orchestrator_with_ledger(tmp_db, engine_with_test_skills):
    """构建一个注入 ledger 的 TaskOrchestrator (用于生命周期测试)。"""
    from core.task_orchestrator import TaskOrchestrator
    from unittest.mock import MagicMock

    ledger = ExecutionLedger(db_path=tmp_db)
    config = {
        "llm": {"default": "test-model"},
        "task_routing": {
            "planner_model": "test-model",
            "classifier_model": "test-model",
            "max_parallel_subtasks": 2,
            "subtask_timeout_minutes": 1,
            "dag_max_depth": 3,
            "dag_max_total_steps": 30,
            "dag_max_total_tokens": 200000,
        },
        "models": {
            "test-model": {"model": "test-model", "provider": "openai"}
        },
    }
    session_mgr = MagicMock()
    session_mgr.save_subtask_dag = MagicMock()
    orch = TaskOrchestrator(
        config=config,
        skill_engine=engine_with_test_skills,
        session_mgr=session_mgr,
        ledger=ledger,
    )
    orch.model_selector = MagicMock()
    orch.model_selector.select.return_value = SimpleNamespace(
        model="test-model", reason="test", fallback_models=()
    )
    return orch, ledger


def test_parent_execution_planning_failure_not_running(tmp_db, engine_with_test_skills):
    """规划失败 (_plan 返回空列表) 时父 execution 应为 failed/planning_failed。"""
    orch, ledger = _build_orchestrator_with_ledger(tmp_db, engine_with_test_skills)

    # mock _plan 返回空列表
    orch._plan = lambda goal, max_steps=None, **kwargs: ([], "")
    # 即便 _classify_and_route 被调用也不应到达，但保险 mock 掉
    orch._classify_and_route = lambda subtasks, **kwargs: None

    result = orch.execute("impossible goal", "sess_planning_fail")

    assert "规划失败" in result
    # 父 execution 应非 running
    recent = ledger.list_recent(limit=5)
    parent_rows = [r for r in recent if r["parent_execution_id"] == ""]
    assert len(parent_rows) == 1
    parent = parent_rows[0]
    assert parent["status"] == "failed"
    assert parent["terminal_reason"] == "planning_failed"
    assert parent["status"] != "running"


def test_parent_execution_failfast_not_running(tmp_db, engine_with_test_skills):
    """Fail-Fast 预算拦截时父 execution 应为 failed/budget_rejected。"""
    from core.subtask_dag import Subtask, SubtaskType
    orch, ledger = _build_orchestrator_with_ledger(tmp_db, engine_with_test_skills)

    # mock _plan 返回大量子任务触发 Fail-Fast (estimated = 10*5=50 > 30*1.5=45)
    fake_subtasks = [
        Subtask(id=f"st{i}", name=f"t{i}", type=SubtaskType.TEXT, prompt="x")
        for i in range(10)
    ]
    orch._plan = lambda goal, max_steps=None, **kwargs: (fake_subtasks, "")
    orch._classify_and_route = lambda subtasks, **kwargs: None

    result = orch.execute("big goal", "sess_failfast")

    assert "任务预算不足" in result
    recent = ledger.list_recent(limit=5)
    parent_rows = [r for r in recent if r["parent_execution_id"] == ""]
    assert len(parent_rows) == 1
    parent = parent_rows[0]
    assert parent["status"] == "failed"
    assert parent["terminal_reason"] == "budget_rejected"
    assert parent["status"] != "running"


def test_parent_execution_exception_not_running(tmp_db, engine_with_test_skills):
    """规划/分类过程抛异常时父 execution 应为 failed/orchestrator_exception。"""
    orch, ledger = _build_orchestrator_with_ledger(tmp_db, engine_with_test_skills)

    # mock _plan 抛异常
    def boom(goal, max_steps=None, **kwargs):
        raise RuntimeError("planner exploded")
    orch._plan = boom

    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="planner exploded"):
        orch.execute("boom goal", "sess_exception")

    recent = ledger.list_recent(limit=5)
    parent_rows = [r for r in recent if r["parent_execution_id"] == ""]
    assert len(parent_rows) == 1
    parent = parent_rows[0]
    assert parent["status"] == "failed"
    assert parent["terminal_reason"] == "orchestrator_exception"
    assert parent["status"] != "running"


# ============================================================
#  Phase 3b: tool_invocations 索引表
# ============================================================

def test_tool_call_result_pairing(ledger, ctx):
    """TOOL_CALL 与 TOOL_RESULT 配对到同一行 (按 tool_call_id)。"""
    execution = ledger.start(ctx, model_name="m")
    recorder = RuntimeRecorder(ledger, execution.id)

    # 记录 TOOL_CALL
    recorder.record(_make_event(RuntimeEventType.TOOL_CALL, {
        "id": "tc_001", "name": "search", "arguments": '{"q": "test"}',
    }))
    # 记录对应 TOOL_RESULT
    recorder.record(_make_event(RuntimeEventType.TOOL_RESULT, {
        "id": "tc_001", "name": "search", "ok": True,
        "output": "result data", "arguments": '{"q": "test"}',
    }))

    invocations = ledger.list_tool_invocations(execution.id)
    assert len(invocations) == 1  # 配对到同一行
    inv = invocations[0]
    assert inv["tool_call_id"] == "tc_001"
    assert inv["tool_name"] == "search"
    assert inv["execution_id"] == execution.id
    assert inv["session_key"] == ctx.session_key
    # 调用侧
    assert inv["arguments_hash"] is not None
    assert inv["arguments_length"] == len('{"q": "test"}')
    # 结果侧
    assert inv["ok"] == 1
    assert inv["output_hash"] is not None
    assert inv["output_length"] == len("result data")
    assert inv["returned_at"] is not None


def test_tool_invocation_idempotent_on_replay(ledger, ctx):
    """重放同一 TOOL_CALL 不产生重复行 (INSERT OR IGNORE)。"""
    execution = ledger.start(ctx, model_name="m")
    recorder = RuntimeRecorder(ledger, execution.id)

    recorder.record(_make_event(RuntimeEventType.TOOL_CALL, {
        "id": "tc_002", "name": "calc", "arguments": "{}",
    }))
    # 重放 (模拟消费端重试)
    recorder._seq = 0
    recorder.record(_make_event(RuntimeEventType.TOOL_CALL, {
        "id": "tc_002", "name": "calc", "arguments": "{}",
    }))

    invocations = ledger.list_tool_invocations(execution.id)
    assert len(invocations) == 1  # 唯一一行


def test_tool_invocation_without_result(ledger, ctx):
    """TOOL_CALL 后执行中断 (无 TOOL_RESULT)，索引行结果侧为空。"""
    execution = ledger.start(ctx, model_name="m")
    recorder = RuntimeRecorder(ledger, execution.id)

    recorder.record(_make_event(RuntimeEventType.TOOL_CALL, {
        "id": "tc_003", "name": "fetch", "arguments": "{}",
    }))

    invocations = ledger.list_tool_invocations(execution.id)
    assert len(invocations) == 1
    inv = invocations[0]
    assert inv["ok"] is None
    assert inv["output_hash"] is None
    assert inv["returned_at"] is None


def test_tool_result_without_prior_call(ledger, ctx):
    """TOOL_RESULT 先于 TOOL_CALL 到达 (异常顺序)，补建索引行。

    使用 recorder 构造的脱敏 payload (含 output_hash/output_length)，
    模拟 TOOL_RESULT 在无对应 TOOL_CALL 时的投影补建。
    """
    import hashlib
    execution = ledger.start(ctx, model_name="m")

    output = "pong"
    ledger.record_and_project(
        execution_id=execution.id, seq=0, event_type="TOOL_RESULT",
        payload={
            "id": "tc_orphan", "name": "ping", "ok": True,
            "output_hash": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "output_length": len(output),
        },
        tool_call_id="tc_orphan", tool_name="ping",
    )

    inv = ledger.get_tool_invocation(execution.id, "tc_orphan")
    assert inv is not None
    assert inv["tool_name"] == "ping"
    assert inv["ok"] == 1
    assert inv["output_length"] == len(output)


def test_tool_invocations_cross_execution_query(ledger, ctx):
    """按工具名跨执行查询，按 session 查询。"""
    # 执行 1
    e1 = ledger.start(ctx, model_name="m")
    r1 = RuntimeRecorder(ledger, e1.id)
    r1.record(_make_event(RuntimeEventType.TOOL_CALL, {
        "id": "tc_a", "name": "search", "arguments": "{}",
    }))
    r1.record(_make_event(RuntimeEventType.TOOL_RESULT, {
        "id": "tc_a", "name": "search", "ok": True, "output": "r1",
    }))

    # 执行 2 (同 session)
    ctx2 = ExecutionContext(
        actor_id="u", actor_type=ActorType.USER,
        source=ExecutionSource.STREAM, session_key=ctx.session_key,
    )
    e2 = ledger.start(ctx2, model_name="m")
    r2 = RuntimeRecorder(ledger, e2.id)
    r2.record(_make_event(RuntimeEventType.TOOL_CALL, {
        "id": "tc_b", "name": "search", "arguments": "{}",
    }))
    r2.record(_make_event(RuntimeEventType.TOOL_RESULT, {
        "id": "tc_b", "name": "search", "ok": False, "output": "err",
    }))

    # 按工具名查 (跨执行)
    by_name = ledger.find_tool_invocations_by_name("search")
    assert len(by_name) == 2
    # 按 session 查 (跨执行)
    by_session = ledger.list_tool_invocations_by_session(ctx.session_key)
    assert len(by_session) == 2


def test_tool_invocation_index_consistent_with_events(ledger, ctx):
    """索引表行数 == TOOL_CALL 事件数 (投影一致性)。"""
    execution = ledger.start(ctx, model_name="m")
    recorder = RuntimeRecorder(ledger, execution.id)

    for i in range(5):
        recorder.record(_make_event(RuntimeEventType.TOOL_CALL, {
            "id": f"tc_{i}", "name": "tool_x", "arguments": "{}",
        }))
        recorder.record(_make_event(RuntimeEventType.TOOL_RESULT, {
            "id": f"tc_{i}", "name": "tool_x", "ok": True,
            "output": f"out_{i}",
        }))

    invocations = ledger.list_tool_invocations(execution.id)
    events = ledger.list_events(execution.id)
    tool_call_events = [e for e in events if e["event_type"] == "TOOL_CALL"]
    tool_result_events = [e for e in events if e["event_type"] == "TOOL_RESULT"]

    # 索引行数 == TOOL_CALL 事件数 == TOOL_RESULT 事件数
    assert len(invocations) == len(tool_call_events) == len(tool_result_events) == 5
    # 每个索引行都有结果
    assert all(inv["ok"] == 1 for inv in invocations)


def test_tool_invocation_secrets_not_stored(ledger, ctx):
    """索引表只存 hash + length，不存原始 arguments/output。"""
    execution = ledger.start(ctx, model_name="m")
    recorder = RuntimeRecorder(ledger, execution.id)

    secret_args = '{"api_key": "sk-secret-12345"}'
    secret_output = "token=sk-leaked-67890"
    recorder.record(_make_event(RuntimeEventType.TOOL_CALL, {
        "id": "tc_secret", "name": "auth", "arguments": secret_args,
    }))
    recorder.record(_make_event(RuntimeEventType.TOOL_RESULT, {
        "id": "tc_secret", "name": "auth", "ok": True,
        "output": secret_output,
    }))

    inv = ledger.get_tool_invocation(execution.id, "tc_secret")
    # 索引行不存原始值
    assert "arguments" not in inv
    assert "output" not in inv
    assert "arguments_redacted" not in inv
    assert "output_preview" not in inv
    # 只存 hash + length
    assert inv["arguments_hash"] is not None
    assert inv["arguments_length"] == len(secret_args)
    assert inv["output_hash"] is not None
    assert inv["output_length"] == len(secret_output)


# ============================================================
#  Phase 3b 修复: P0 复合唯一键 / P1 异常顺序回填 / P1 step+duration 贯通
# ============================================================

def test_two_executions_same_tool_call_id_not_overwriting(ledger, ctx):
    """两个 execution 使用相同 tool_call_id 必须产生两个独立索引行，结果不得互相覆盖。

    P0: tool_call_id 不可全局唯一，UNIQUE(execution_id, tool_call_id)。
    """
    import hashlib
    # 执行 1
    e1 = ledger.start(ctx, model_name="m")
    r1 = RuntimeRecorder(ledger, e1.id)
    r1.record(_make_event(RuntimeEventType.TOOL_CALL, {
        "id": "shared_tc", "name": "search", "arguments": '{"q":"a"}',
        "step": 0,
    }))
    r1.record(_make_event(RuntimeEventType.TOOL_RESULT, {
        "id": "shared_tc", "name": "search", "ok": True, "output": "result_A",
        "step": 0, "duration_ms": 10,
    }))

    # 执行 2 (同 ID)
    e2 = ledger.start(ctx, model_name="m")
    r2 = RuntimeRecorder(ledger, e2.id)
    r2.record(_make_event(RuntimeEventType.TOOL_CALL, {
        "id": "shared_tc", "name": "search", "arguments": '{"q":"b"}',
        "step": 0,
    }))
    r2.record(_make_event(RuntimeEventType.TOOL_RESULT, {
        "id": "shared_tc", "name": "search", "ok": False, "output": "result_B",
        "step": 0, "duration_ms": 20,
    }))

    # 两个独立索引行
    inv1 = ledger.get_tool_invocation(e1.id, "shared_tc")
    inv2 = ledger.get_tool_invocation(e2.id, "shared_tc")
    assert inv1 is not None and inv2 is not None
    # 结果不互相覆盖
    assert inv1["ok"] == 1
    assert inv1["output_length"] == len("result_A")
    assert inv2["ok"] == 0
    assert inv2["output_length"] == len("result_B")
    # 两个不同的 execution
    assert inv1["execution_id"] == e1.id
    assert inv2["execution_id"] == e2.id


def test_abnormal_order_full_backfill(ledger, ctx):
    """异常顺序: TOOL_RESULT 先到，之后 TOOL_CALL 到达，调用侧必须完整回填。

    P1: TOOL_CALL 使用 UPSERT，补齐调用侧字段，不覆盖已存在的结果侧字段。
    """
    import hashlib
    execution = ledger.start(ctx, model_name="m")

    # 1) TOOL_RESULT 先到 (异常顺序)
    output = "pong"
    ledger.record_and_project(
        execution_id=execution.id, seq=0, event_type="TOOL_RESULT",
        payload={
            "id": "tc_ab", "name": "ping", "ok": True,
            "output_hash": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "output_length": len(output),
            "duration_ms": 15, "step": 1,
        },
        tool_call_id="tc_ab", tool_name="ping",
    )

    inv_after_result = ledger.get_tool_invocation(execution.id, "tc_ab")
    assert inv_after_result["ok"] == 1
    assert inv_after_result["output_length"] == len(output)
    # 调用侧为空
    assert inv_after_result["arguments_hash"] is None
    assert inv_after_result["arguments_length"] is None

    # 2) TOOL_CALL 后到 (正常应有数据)
    args = '{"q":"hi"}'
    ledger.record_and_project(
        execution_id=execution.id, seq=1, event_type="TOOL_CALL",
        payload={
            "id": "tc_ab", "name": "ping",
            "arguments_hash": hashlib.sha256(args.encode("utf-8")).hexdigest(),
            "arguments_length": len(args), "step": 1,
        },
        tool_call_id="tc_ab", tool_name="ping",
        step=1,
    )

    inv_after_call = ledger.get_tool_invocation(execution.id, "tc_ab")
    # 调用侧已回填
    assert inv_after_call["arguments_hash"] == hashlib.sha256(
        args.encode("utf-8")).hexdigest()
    assert inv_after_call["arguments_length"] == len(args)
    assert inv_after_call["step"] == 1
    # 结果侧未被覆盖
    assert inv_after_call["ok"] == 1
    assert inv_after_call["output_length"] == len(output)
    assert inv_after_call["duration_ms"] == 15


def test_step_and_duration_ms_populated_from_runtime(tmp_db, engine_with_test_skills):
    """端到端: AgentRuntime 发出的事件携带 step + duration_ms，
    RuntimeRecorder 投影到 tool_invocations 后二者非空。"""
    from unittest.mock import MagicMock, patch
    from core.agent_runtime import AgentRuntime
    from core.model_invoker import OpenAIInvoker

    ledger = ExecutionLedger(db_path=tmp_db)
    ctx = ExecutionContext(
        actor_id="u", actor_type=ActorType.USER,
        source=ExecutionSource.STREAM, session_key="sess_e2e",
        max_steps=3, token_budget=10000,
    )
    execution = ledger.start(ctx, model_name="test-model")
    recorder = RuntimeRecorder(ledger, execution.id)

    invoker = OpenAIInvoker(client=MagicMock(), model_name="test-model")
    runtime = AgentRuntime(
        model_invoker=invoker,
        skill_engine=engine_with_test_skills,
        max_steps=3,
        max_tokens=2048,
    )
    messages = [{"role": "user", "content": "请调用 echo 工具说 hi"}]
    tools = engine_with_test_skills.get_all_schemas()

    call_count = [0]

    def mock_invoke(messages, tools, **kwargs):
        if call_count[0] == 0:
            call_count[0] += 1
            return {
                "content": "",
                "tool_calls": [{
                    "id": "call_e2e_1",
                    "name": "test_echo",
                    "arguments": '{"text":"hi"}',
                }],
                "finish_reason": "tool_calls",
                "usage_total": 50,
            }
        return {
            "content": "done", "tool_calls": [],
            "finish_reason": "stop", "usage_total": 60,
        }

    with patch.object(invoker, "invoke_sync", side_effect=mock_invoke):
        for event in recorder.wrap(runtime.run(messages, tools, ctx, stream=False)):
            pass  # 消费完事件

    invocations = ledger.list_tool_invocations(execution.id)
    assert len(invocations) >= 1
    inv = invocations[0]
    # step 和 duration_ms 必须非空且合理
    assert inv["step"] is not None
    assert inv["step"] >= 0
    assert inv["duration_ms"] is not None
    assert inv["duration_ms"] >= 0


# ============================================================
#  Phase 3b 迁移: v1 → v2 复合唯一键升级
# ============================================================

_V1_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id        TEXT PRIMARY KEY,
    parent_execution_id TEXT,
    session_key         TEXT,
    actor_id            TEXT NOT NULL,
    actor_type          TEXT NOT NULL,
    source              TEXT NOT NULL,
    model_name          TEXT,
    provider            TEXT,
    stream_mode         INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,
    terminal_reason     TEXT,
    max_steps           INTEGER,
    max_output_tokens   INTEGER,
    token_budget        INTEGER,
    steps_used          INTEGER NOT NULL DEFAULT 0,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    started_at          REAL NOT NULL,
    finished_at         REAL,
    duration_ms         INTEGER,
    created_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id    TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    step            INTEGER,
    tool_call_id    TEXT,
    tool_name       TEXT,
    payload_json    TEXT NOT NULL,
    created_at      REAL NOT NULL,
    UNIQUE(execution_id, seq),
    FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
);

-- v1 建表: tool_call_id 全局 UNIQUE (错误假设)
CREATE TABLE IF NOT EXISTS tool_invocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_call_id    TEXT NOT NULL UNIQUE,
    execution_id    TEXT NOT NULL,
    session_key     TEXT,
    step            INTEGER,
    tool_name       TEXT NOT NULL,
    arguments_hash  TEXT,
    arguments_length INTEGER,
    ok              INTEGER,
    output_hash     TEXT,
    output_length   INTEGER,
    duration_ms     INTEGER,
    called_at       REAL NOT NULL,
    returned_at     REAL,
    FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO schema_meta (key, value) VALUES ('version', '1');
"""


def _create_v1_database(db_path: str) -> None:
    """创建一个 v1 旧数据库，并插入一行 v1 格式的 tool_invocations 数据。"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.executescript(_V1_SCHEMA_SQL)
    # 插入一条执行 + 一条工具调用索引行
    conn.execute(
        "INSERT INTO executions (execution_id, session_key, actor_id, "
        "actor_type, source, status, started_at, model_name, stream_mode, created_at) "
        "VALUES (?, ?, 'u', 'USER', 'STREAM', 'succeeded', ?, 'm', 0, ?)",
        ("exec_legacy_1", "sess_legacy", 1000.0, 1000.0),
    )
    conn.execute(
        "INSERT INTO tool_invocations "
        "(tool_call_id, execution_id, session_key, step, tool_name, "
        " arguments_hash, arguments_length, ok, output_hash, output_length, "
        " duration_ms, called_at, returned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("tc_legacy", "exec_legacy_1", "sess_legacy", 0, "search",
         "abc123", 10, 1, "def456", 20, 5, 1001.0, 1002.0),
    )
    conn.commit()
    conn.close()


def test_v1_to_v2_migration_preserves_data(tmp_db):
    """v1 → v2 迁移: 旧数据保留，schema version 升为 2，
    两个 execution 可以写入相同 tool_call_id。
    """
    import sqlite3
    # 1) 创建 v1 数据库
    _create_v1_database(tmp_db)

    # 2) 初始化新版 Ledger (触发迁移)
    ledger = ExecutionLedger(db_path=tmp_db)

    # 3) 原有数据保留
    with sqlite3.connect(tmp_db) as conn:
        # 版本为 2
        cur = conn.execute("SELECT value FROM schema_meta WHERE key='version'")
        assert cur.fetchone()[0] == "2"
        # tool_invocations_v1 已被删除
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='tool_invocations_v1'")
        assert cur.fetchone() is None
        # 旧数据行保留
        cur = conn.execute(
            "SELECT tool_call_id, execution_id, tool_name, ok FROM tool_invocations")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0] == ("tc_legacy", "exec_legacy_1", "search", 1)

    # 4) 两个 execution 可以写入相同 tool_call_id (P0 复合唯一键生效)
    ctx = ExecutionContext(
        actor_id="u", actor_type=ActorType.USER,
        source=ExecutionSource.STREAM, session_key="sess_new",
        max_steps=3, token_budget=10000,
    )
    e1 = ledger.start(ctx, model_name="m")
    e2 = ledger.start(ctx, model_name="m")

    ledger.record_and_project(
        execution_id=e1.id, seq=0, event_type="TOOL_CALL",
        payload={"id": "shared_id", "name": "tool", "arguments_hash": "h1",
                 "arguments_length": 5, "step": 0},
        tool_call_id="shared_id", tool_name="tool", step=0,
    )
    ledger.record_and_project(
        execution_id=e2.id, seq=0, event_type="TOOL_CALL",
        payload={"id": "shared_id", "name": "tool", "arguments_hash": "h2",
                 "arguments_length": 6, "step": 0},
        tool_call_id="shared_id", tool_name="tool", step=0,
    )

    inv1 = ledger.get_tool_invocation(e1.id, "shared_id")
    inv2 = ledger.get_tool_invocation(e2.id, "shared_id")
    assert inv1 is not None and inv2 is not None
    assert inv1["arguments_length"] == 5
    assert inv2["arguments_length"] == 6


def test_v1_to_v2_migration_idempotent(tmp_db):
    """重复初始化不会再次迁移，schema version 保持 2。"""
    import sqlite3
    _create_v1_database(tmp_db)

    # 第一次初始化: 触发迁移
    ledger1 = ExecutionLedger(db_path=tmp_db)
    with sqlite3.connect(tmp_db) as conn:
        cur = conn.execute("SELECT value FROM schema_meta WHERE key='version'")
        assert cur.fetchone()[0] == "2"

    # 采集当前表结构指纹
    with sqlite3.connect(tmp_db) as conn:
        cur = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='tool_invocations'")
        schema_after_first = cur.fetchone()[0]

    # 第二次初始化: 不应再次迁移
    ledger2 = ExecutionLedger(db_path=tmp_db)
    with sqlite3.connect(tmp_db) as conn:
        cur = conn.execute("SELECT value FROM schema_meta WHERE key='version'")
        assert cur.fetchone()[0] == "2"
        cur = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='tool_invocations'")
        schema_after_second = cur.fetchone()[0]
        # 表结构未变 (幂等)
        assert schema_after_first == schema_after_second


def test_v1_to_v2_upsert_works_on_migrated_db(tmp_db):
    """迁移后的数据库 UPSERT 正常工作 (ON CONFLICT(execution_id, tool_call_id))。"""
    _create_v1_database(tmp_db)
    ledger = ExecutionLedger(db_path=tmp_db)

    ctx = ExecutionContext(
        actor_id="u", actor_type=ActorType.USER,
        source=ExecutionSource.STREAM, session_key="sess_upsert",
        max_steps=3, token_budget=10000,
    )
    execution = ledger.start(ctx, model_name="m")

    # TOOL_RESULT 先到 (异常顺序)
    ledger.record_and_project(
        execution_id=execution.id, seq=0, event_type="TOOL_RESULT",
        payload={"id": "tc_x", "name": "ping", "ok": True,
                 "output_hash": "h", "output_length": 4,
                 "duration_ms": 10, "step": 0},
        tool_call_id="tc_x", tool_name="ping",
    )
    # TOOL_CALL 后到 (回填调用侧)
    ledger.record_and_project(
        execution_id=execution.id, seq=1, event_type="TOOL_CALL",
        payload={"id": "tc_x", "name": "ping",
                 "arguments_hash": "ah", "arguments_length": 5, "step": 0},
        tool_call_id="tc_x", tool_name="ping", step=0,
    )

    inv = ledger.get_tool_invocation(execution.id, "tc_x")
    assert inv is not None
    assert inv["arguments_length"] == 5
    assert inv["ok"] == 1
    assert inv["duration_ms"] == 10


# ============================================================
#  Phase 3b 迁移: 故障注入测试 (半迁移恢复)
# ============================================================

class _FaultInjectingConnection:
    """包装 sqlite3.Connection，在指定 SQL 模式首次匹配时抛异常。

    用于验证迁移的 BEGIN IMMEDIATE 事务性: 故障后 ROLLBACK，旧表不变。
    """

    def __init__(self, real_conn, fault_on_pattern: str):
        self._real = real_conn
        self._fault_on = fault_on_pattern
        self._triggered = False

    @property
    def triggered(self) -> bool:
        return self._triggered

    def execute(self, sql, *args, **kwargs):
        if (not self._triggered and self._fault_on
                and self._fault_on in sql):
            self._triggered = True
            raise sqlite3.OperationalError(
                f"injected fault on: {self._fault_on}")
        return self._real.execute(sql, *args, **kwargs)

    def commit(self):
        return self._real.commit()

    def close(self):
        return self._real.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self._real.__exit__(*args)


def _run_migration_with_fault(tmp_db, fault_pattern: str) -> bool:
    """用故障注入连接初始化 ExecutionLedger，返回故障是否触发。"""
    import sqlite3 as _sqlite3
    real_ledger_cls = ExecutionLedger
    fault_conn_holder = {}

    original_connect = real_ledger_cls._connect

    def faulty_connect(self):
        real = original_connect(self)
        wrapper = _FaultInjectingConnection(real, fault_pattern)
        fault_conn_holder["conn"] = wrapper
        return wrapper

    real_ledger_cls._connect = faulty_connect
    try:
        real_ledger_cls(db_path=tmp_db)
    except Exception:
        pass
    finally:
        real_ledger_cls._connect = original_connect

    return fault_conn_holder.get("conn") is not None and \
        fault_conn_holder["conn"].triggered


def test_migration_rollback_on_create_failure(tmp_db):
    """CREATE TABLE 步骤抛异常 → ROLLBACK → 旧表不变 → 重新初始化成功。

    验证 BEGIN IMMEDIATE 单事务: CREATE 失败回滚后，RENAME 也回滚，
    旧表保持原名和数据，重新初始化可正常迁移。
    """
    import sqlite3
    _create_v1_database(tmp_db)

    triggered = _run_migration_with_fault(tmp_db, "CREATE TABLE tool_invocations")
    assert triggered, "故障未触发"

    # 验证 ROLLBACK 生效: 旧表状态完好
    with sqlite3.connect(tmp_db) as conn:
        cur = conn.execute("SELECT value FROM schema_meta WHERE key='version'")
        assert cur.fetchone()[0] == "1"  # 版本未升级
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='tool_invocations_v1'")
        assert cur.fetchone() is None  # RENAME 已回滚
        cur = conn.execute("SELECT COUNT(*) FROM tool_invocations")
        assert cur.fetchone()[0] == 1  # 旧数据保留

    # 重新初始化 (无故障): 迁移应成功
    ledger = ExecutionLedger(db_path=tmp_db)
    with sqlite3.connect(tmp_db) as conn:
        cur = conn.execute("SELECT value FROM schema_meta WHERE key='version'")
        assert cur.fetchone()[0] == "2"
        cur = conn.execute("SELECT COUNT(*) FROM tool_invocations")
        assert cur.fetchone()[0] == 1  # 旧数据仍保留


def test_migration_rollback_on_copy_failure(tmp_db):
    """INSERT...SELECT (COPY) 步骤抛异常 → ROLLBACK → 旧表不变 → 重新初始化成功。"""
    import sqlite3
    _create_v1_database(tmp_db)

    triggered = _run_migration_with_fault(tmp_db, "INSERT INTO tool_invocations")
    assert triggered, "故障未触发"

    # 验证 ROLLBACK 生效: 旧表状态完好
    with sqlite3.connect(tmp_db) as conn:
        cur = conn.execute("SELECT value FROM schema_meta WHERE key='version'")
        assert cur.fetchone()[0] == "1"
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='tool_invocations_v1'")
        assert cur.fetchone() is None
        cur = conn.execute("SELECT COUNT(*) FROM tool_invocations")
        assert cur.fetchone()[0] == 1

    # 重新初始化 (无故障): 迁移成功
    ledger = ExecutionLedger(db_path=tmp_db)
    with sqlite3.connect(tmp_db) as conn:
        cur = conn.execute("SELECT value FROM schema_meta WHERE key='version'")
        assert cur.fetchone()[0] == "2"
        cur = conn.execute("SELECT COUNT(*) FROM tool_invocations")
        assert cur.fetchone()[0] == 1


def test_migration_version_not_upgraded_on_failure(tmp_db):
    """DROP 步骤抛异常 → ROLLBACK → schema version 保持 1 → 重新初始化成功。"""
    import sqlite3
    _create_v1_database(tmp_db)

    triggered = _run_migration_with_fault(tmp_db, "DROP TABLE tool_invocations_v1")
    assert triggered

    with sqlite3.connect(tmp_db) as conn:
        cur = conn.execute("SELECT value FROM schema_meta WHERE key='version'")
        assert cur.fetchone()[0] == "1"  # 未升级

    # 重新初始化 (无故障): 迁移成功，数据完整
    ledger = ExecutionLedger(db_path=tmp_db)
    with sqlite3.connect(tmp_db) as conn:
        cur = conn.execute("SELECT value FROM schema_meta WHERE key='version'")
        assert cur.fetchone()[0] == "2"
        cur = conn.execute("SELECT COUNT(*) FROM tool_invocations")
        assert cur.fetchone()[0] == 1

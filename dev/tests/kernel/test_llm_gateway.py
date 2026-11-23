from unittest.mock import MagicMock

import pytest

from lite_agent.core.execution_ledger import ExecutionLedger
from lite_agent.core.llm_gateway import LLMGateway
from lite_agent.core.execution_budget import ExecutionBudget, BudgetAdmissionError


def test_gateway_routes_and_invokes_without_ledger():
    router = MagicMock()
    invoker = MagicMock()
    invoker.invoke_sync.return_value = {"content": "ok", "usage_total": 3}
    router.get_invoker.return_value = invoker

    result = LLMGateway(router).invoke_sync(
        [{"role": "user", "content": "hello"}], model="flash", max_tokens=50
    )

    assert result["content"] == "ok"
    router.get_invoker.assert_called_once_with("flash", max_tokens=50)
    invoker.invoke_sync.assert_called_once()


def test_gateway_records_usage_and_parent(tmp_path):
    ledger = ExecutionLedger(str(tmp_path / "ledger.db"))
    invoker = MagicMock()
    invoker.model_name = "flash-actual"
    invoker.max_tokens = 100
    invoker.invoke_sync.return_value = {
        "content": "ok", "finish_reason": "stop", "usage_total": 12,
        "prompt_tokens": 7, "completion_tokens": 5,
    }

    LLMGateway(ledger=ledger).invoke_sync(
        [{"role": "user", "content": "hello"}], invoker=invoker,
        role="test_role", session_key="s1", parent_execution_id="parent-1",
    )

    rows = [x for x in ledger.list_recent() if x["session_key"] == "s1"]
    assert len(rows) == 1
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["parent_execution_id"] == "parent-1"
    assert rows[0]["total_tokens"] == 12
    assert rows[0]["prompt_tokens"] == 7
    assert rows[0]["completion_tokens"] == 5


def test_gateway_records_failure(tmp_path):
    ledger = ExecutionLedger(str(tmp_path / "ledger.db"))
    invoker = MagicMock()
    invoker.model_name = "broken"
    invoker.max_tokens = 100
    invoker.invoke_sync.side_effect = RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        LLMGateway(ledger=ledger).invoke_sync(
            [{"role": "user", "content": "hello"}], invoker=invoker,
            session_key="s2",
        )

    rows = [x for x in ledger.list_recent() if x["session_key"] == "s2"]
    assert rows[0]["status"] == "failed"


@pytest.mark.parametrize("with_ledger", [False, True])
def test_retry_calls_adapter_once_per_attempt_and_preserves_failed_execution(tmp_path, monkeypatch, with_ledger):
    import json
    ledger = ExecutionLedger(str(tmp_path / "attempts.db")) if with_ledger else None
    invoker = MagicMock(model_name="test-model", max_tokens=16)
    invoker.invoke_sync.side_effect = [RuntimeError("503 service unavailable"),
                                       {"content": "ok", "usage_total": 7}]
    sleeps = []
    monkeypatch.setattr("lite_agent.core.llm_gateway.time.sleep", sleeps.append)
    result = LLMGateway(ledger=ledger).invoke_sync(
        [{"role": "user", "content": "test"}], invoker=invoker, max_retries=1,
    )
    assert result["content"] == "ok"
    assert invoker.invoke_sync.call_count == 2
    assert all(call.kwargs["max_retries"] == 0 for call in invoker.invoke_sync.call_args_list)
    assert sleeps == [0.2]
    if ledger:
        rows = ledger.list_recent()
        assert sorted(row["status"] for row in rows) == ["failed", "succeeded"]
        starts = [json.loads(event["payload_json"])
                  for row in rows for event in ledger.list_events(row["execution_id"])
                  if event["event_type"] == "STEP_START"]
        assert sorted(start["attempt"] for start in starts) == [1, 2]
        assert len({start["invocation_id"] for start in starts}) == 1


def test_retry_reserves_and_accounts_each_attempt(tmp_path, monkeypatch):
    budget = ExecutionBudget(max_steps=2, max_tokens=10000)
    ledger = ExecutionLedger(str(tmp_path / "budget-attempts.db"))
    invoker = MagicMock(model_name="test-model", max_tokens=16)
    invoker.invoke_sync.side_effect = [RuntimeError("503"), {"content": "ok", "usage_total": 5}]
    monkeypatch.setattr("lite_agent.core.llm_gateway.time.sleep", lambda _: None)
    LLMGateway(budget=budget, ledger=ledger).invoke_sync(
        [{"role": "user", "content": "test"}], invoker=invoker,
        max_tokens=16, max_retries=1,
    )
    snapshot = budget.snapshot()
    assert snapshot.used_steps == 2
    assert snapshot.estimated_tokens > 0
    assert snapshot.used_tokens == snapshot.estimated_tokens + 5
    assert sum(row["total_tokens"] for row in ledger.list_recent()) == snapshot.used_tokens
    assert snapshot.reserved_tokens == 0


def test_retry_cannot_exceed_remaining_budget(monkeypatch):
    budget = ExecutionBudget(max_steps=1, max_tokens=10000)
    invoker = MagicMock()
    invoker.invoke_sync.side_effect = RuntimeError("503")
    monkeypatch.setattr("lite_agent.core.llm_gateway.time.sleep", lambda _: None)
    with pytest.raises(BudgetAdmissionError):
        LLMGateway(budget=budget).invoke_sync(
            [{"role": "user", "content": "test"}], invoker=invoker,
            max_tokens=16, max_retries=1,
        )
    assert invoker.invoke_sync.call_count == 1
    assert budget.snapshot().reserved_tokens == 0


@pytest.mark.parametrize("error", ["401 Unauthorized", "403 Forbidden", "429 insufficient_quota"])
def test_non_retryable_sync_error_uses_one_attempt(monkeypatch, error):
    invoker = MagicMock()
    invoker.invoke_sync.side_effect = RuntimeError(error)
    sleep = MagicMock()
    monkeypatch.setattr("lite_agent.core.llm_gateway.time.sleep", sleep)
    with pytest.raises(RuntimeError):
        LLMGateway().invoke_sync([], invoker=invoker, max_retries=2)
    assert invoker.invoke_sync.call_count == 1
    sleep.assert_not_called()


def test_retry_after_cannot_exceed_total_timeout(monkeypatch):
    from types import SimpleNamespace
    invoker = MagicMock()
    error = RuntimeError("429 Too many requests")
    error.response = SimpleNamespace(headers={"retry-after": "60"})
    invoker.invoke_sync.side_effect = error
    sleep = MagicMock()
    monkeypatch.setattr("lite_agent.core.llm_gateway.time.sleep", sleep)
    with pytest.raises(RuntimeError):
        LLMGateway().invoke_sync([], invoker=invoker, max_retries=2, timeout=1)
    assert invoker.invoke_sync.call_count == 1
    sleep.assert_not_called()


def test_stream_disables_adapter_retries_without_budget():
    invoker = MagicMock()
    invoker.invoke_stream.return_value = iter(())
    list(LLMGateway().invoke_stream([], [], invoker=invoker, max_retries=3))
    assert invoker.invoke_stream.call_args.kwargs["max_retries"] == 0


def test_stream_retry_records_failed_attempt_and_respects_retry_after(monkeypatch):
    from types import SimpleNamespace
    from lite_agent.core.agent_runtime import AgentRuntime, RuntimeState, RuntimeEventType
    from lite_agent.core.model_event import ModelEvent, ModelEventType
    error = RuntimeError("429 Too many requests")
    error.response = SimpleNamespace(headers={"retry-after": "2"})
    invoker = MagicMock()
    invoker.invoke_stream.side_effect = [
        iter([ModelEvent(ModelEventType.ERROR, error)]),
        iter([ModelEvent(ModelEventType.TEXT, "ok"), ModelEvent(ModelEventType.DONE, {})]),
    ]
    sleeps = []
    monkeypatch.setattr("lite_agent.core.agent_runtime.time.sleep", sleeps.append)
    runtime = AgentRuntime(invoker, MagicMock(), max_retries=1)
    events = list(runtime._consume_stream(RuntimeState(), [], {}, {}, 60, 16))
    attempts = [event.data for event in events
                if event.type == RuntimeEventType.BUDGET_DECISION]
    assert [event["status"] for event in attempts] == ["started", "failed", "started", "succeeded"]
    assert len({event["invocation_id"] for event in attempts}) == 1
    assert attempts[1]["usage_unknown"] is True
    assert sleeps == [2]
    assert invoker.invoke_stream.call_count == 2
    assert all(call.kwargs["max_retries"] == 0 for call in invoker.invoke_stream.call_args_list)


def test_sync_runtime_uses_gateway_retry_policy(monkeypatch):
    from lite_agent.core.agent_runtime import AgentRuntime, RuntimeEventType
    from lite_agent.contracts.execution import ExecutionContext
    invoker = MagicMock()
    invoker.invoke_sync.side_effect = [RuntimeError("503 service unavailable"),
        {"content": "ok", "tool_calls": [], "usage_total": 5, "finish_reason": "stop"}]
    monkeypatch.setattr("lite_agent.core.llm_gateway.time.sleep", lambda _: None)
    events = list(AgentRuntime(invoker, MagicMock(), max_retries=1).run(
        [{"role": "user", "content": "test"}], [],
        ExecutionContext(actor_id="test", max_steps=1), timeout=10, stream=False,
    ))
    assert any(event.type == RuntimeEventType.TEXT and event.data == "ok" for event in events)
    assert invoker.invoke_sync.call_count == 2
    assert all(call.kwargs["max_retries"] == 0 for call in invoker.invoke_sync.call_args_list)


@pytest.mark.parametrize("code, expected_calls", [("rate_limit_exceeded", 2), ("insufficient_quota", 1)])
def test_chat_sdk_429_uses_error_code_instead_of_blanket_rejection(monkeypatch, code, expected_calls):
    import httpx
    import openai
    from lite_agent.agent import _NON_RETRYABLE_EXCEPTIONS
    from lite_agent.core.agent_runtime import AgentRuntime, RuntimeState
    from lite_agent.core.model_event import ModelEvent, ModelEventType
    response = httpx.Response(429, request=httpx.Request("POST", "https://example.invalid/v1"))
    error = openai.RateLimitError(code, response=response, body={"code": code})
    invoker = MagicMock()
    invoker.invoke_stream.side_effect = lambda *a, **k: iter([ModelEvent(ModelEventType.ERROR, error)])
    monkeypatch.setattr("lite_agent.core.agent_runtime.time.sleep", lambda _: None)
    runtime = AgentRuntime(invoker, MagicMock(), max_retries=1,
                           non_retryable_exceptions=_NON_RETRYABLE_EXCEPTIONS)
    with pytest.raises(openai.RateLimitError):
        list(runtime._consume_stream(RuntimeState(), [], {}, {}, 60, 16))
    assert invoker.invoke_stream.call_count == expected_calls

from unittest.mock import MagicMock

import pytest

from core.execution_ledger import ExecutionLedger
from core.llm_gateway import LLMGateway


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

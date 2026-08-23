import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.execution import ExecutionResult
from core.model_invoker import GeminiInvoker, OpenAIInvoker
from core.model_router import ModelRouter
from core.subtask_dag import Subtask, SubtaskDAG
from core.task_orchestrator import TaskOrchestrator
from agent import Agent


def test_model_router_builds_invoker_for_each_driver():
    router = ModelRouter.__new__(ModelRouter)
    router.models_cfg = {
        "open": {"model": "open-actual", "temperature": 0.2, "max_tokens": 99},
        "gem": {"model": "gem-actual", "temperature": 0.1, "max_tokens": 88},
    }
    router._clients = {"open": MagicMock(), "gem": MagicMock()}
    router._drivers = {"open": "openai", "gem": "gemini_native"}

    open_invoker = router.get_invoker("open")
    gem_invoker = router.get_invoker("gem")

    assert isinstance(open_invoker, OpenAIInvoker)
    assert open_invoker.model_name == "open-actual"
    assert open_invoker.max_tokens == 99
    assert isinstance(gem_invoker, GeminiInvoker)
    assert gem_invoker.model_name == "gem-actual"


def _orchestrator_for_direct_execution(enabled=True):
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.direct_tool_execution = enabled
    orch.skill_engine = MagicMock()
    orch.skill_engine.get_all_names.return_value = {"ops_sys_status"}
    orch.skill_engine.execute_with_context.return_value = ExecutionResult(
        ok=True,
        output="healthy",
        tool_name="ops_sys_status",
    )
    orch._log_and_persist = MagicMock()
    return orch


def test_direct_tool_node_skips_worker_and_uses_strict_allowlist():
    orch = _orchestrator_for_direct_execution()
    subtask = Subtask(
        id="sub_1",
        name="read status",
        execution_mode="tool",
        tool_name="ops_sys_status",
        tool_arguments={"host": "vps1"},
        tools=["ops_sys_status"],
    )
    results = {}

    orch._run_direct_tool_subtask(subtask, results, threading.Lock())

    assert results["sub_1"]["status"] == "done"
    assert results["sub_1"]["token_usage"] == 0
    ctx, name, arguments = orch.skill_engine.execute_with_context.call_args.args
    assert ctx.allowed_tools == frozenset({"ops_sys_status"})
    assert name == "ops_sys_status"
    assert json.loads(arguments) == {"host": "vps1"}


def test_direct_tool_node_falls_back_when_disabled_or_invalid():
    orch = _orchestrator_for_direct_execution(enabled=False)
    valid = Subtask(
        id="sub_1", name="status", execution_mode="tool",
        tool_name="ops_sys_status", tool_arguments={}, tools=["ops_sys_status"],
    )
    assert orch._can_execute_directly(valid) is False

    orch.direct_tool_execution = True
    valid.tool_name = "invented_tool"
    assert orch._can_execute_directly(valid) is False


def test_subtask_direct_fields_round_trip_and_old_data_defaults_to_agent():
    direct = Subtask(
        id="sub_1", name="status", execution_mode="tool",
        tool_name="ops_sys_status", tool_arguments={"host": "vps1"},
    )
    restored = SubtaskDAG.from_dict(SubtaskDAG([direct]).to_dict()).subtasks["sub_1"]
    assert restored.execution_mode == "tool"
    assert restored.tool_name == "ops_sys_status"
    assert restored.tool_arguments == {"host": "vps1"}

    old = SubtaskDAG.from_dict([{"id": "old", "name": "legacy"}]).subtasks["old"]
    assert old.execution_mode == "agent"


def test_planner_uses_selector_subset_in_prompt(monkeypatch):
    monkeypatch.setenv("LITE_AGENT_SELECTOR_ENABLED", "1")
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.dag_max_steps = 30
    orch.planner_model = "planner"
    orch.config = {"llm": {"default": "planner"}}
    orch.router = MagicMock()
    invoker = MagicMock()
    invoker.model_name = "planner-actual"
    invoker.invoke_sync.return_value = {
        "content": json.dumps({"global_strategy": "", "subtasks": []}),
        "finish_reason": "stop",
        "usage_total": 10,
    }
    orch.router.get_invoker.return_value = invoker
    orch.request_selector = MagicMock()
    orch.request_selector.select.return_value = SimpleNamespace(
        names=["ops_sys_status"], confidence="high"
    )
    orch.skill_engine = MagicMock()
    orch.skill_engine.get_schemas_by_names.return_value = [{
        "function": {"name": "ops_sys_status", "description": "status"}
    }]

    orch._plan("inspect vps1")

    orch.skill_engine.get_schemas_by_names.assert_called_once_with(["ops_sys_status"])
    prompt = invoker.invoke_sync.call_args.kwargs["messages"][0]["content"]
    assert "ops_sys_status" in prompt
    orch.skill_engine.get_all_schemas.assert_not_called()


def test_planner_shadow_keeps_full_tool_catalogue(monkeypatch):
    monkeypatch.setenv("LITE_AGENT_SELECTOR_SHADOW", "1")
    monkeypatch.delenv("LITE_AGENT_SELECTOR_ENABLED", raising=False)
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.dag_max_steps = 30
    orch.planner_model = "planner"
    orch.config = {"llm": {"default": "planner"}}
    orch.router = MagicMock()
    invoker = MagicMock(model_name="planner-actual")
    invoker.invoke_sync.return_value = {
        "content": json.dumps({"subtasks": []}),
        "finish_reason": "stop",
        "usage_total": 1,
    }
    orch.router.get_invoker.return_value = invoker
    orch.request_selector = MagicMock()
    orch.request_selector.select.return_value = SimpleNamespace(
        names=["ops_sys_status"], confidence="high"
    )
    orch.skill_engine = MagicMock()
    orch.skill_engine.get_all_schemas.return_value = [{
        "function": {"name": "all_tool", "description": "all"}
    }]

    orch._plan("inspect vps1")

    orch.skill_engine.get_all_schemas.assert_called_once()
    orch.skill_engine.get_schemas_by_names.assert_not_called()


def test_explicit_model_override_supports_bracket_and_natural_alias():
    agent = Agent.__new__(Agent)
    agent._config = {"llm": {"models": {
        "gemini-flash": {}, "gemini-pro": {},
    }}}

    assert agent._extract_model_override(
        "用 gemini flash 来清理空间"
    ) == "gemini-flash"
    assert agent._extract_model_override(
        "搜索产品 [model=gemini-pro]"
    ) == "gemini-pro"
    assert agent._extract_model_override("普通聊天") is None


def test_unknown_bracket_model_is_rejected():
    agent = Agent.__new__(Agent)
    agent._config = {"llm": {"models": {"flash": {}}}}

    import pytest
    with pytest.raises(ValueError, match="未配置模型"):
        agent._extract_model_override("执行任务 [model=not-real]")

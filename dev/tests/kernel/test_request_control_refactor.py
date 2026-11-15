import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from lite_agent.contracts.execution import ExecutionResult
from lite_agent.core.model_invoker import GeminiInvoker, OpenAIInvoker
from lite_agent.core.model_router import ModelRouter
from lite_agent.core.model_policy import ExecutionPolicy, ModelSelector
from lite_agent.core.subtask_dag import Subtask, SubtaskDAG
from lite_agent.core.task_orchestrator import TaskOrchestrator
from lite_agent.core.worker_agent import WorkerOutcome
from lite_agent.agent import Agent


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


def test_model_call_profile_merges_default_and_named_settings():
    router = ModelRouter.__new__(ModelRouter)
    router.models_cfg = {
        "glm": {
            "temperature": 0.2,
            "max_tokens": 8192,
            "profiles": {
                "default": {
                    "timeout": 90,
                    "invoke_kwargs": {"thinking": {"type": "auto"}},
                },
                "structured_json": {
                    "max_tokens": 6000,
                    "max_retries": 1,
                    "invoke_kwargs": {
                        "response_format": {"type": "json_object"}
                    },
                },
            },
        },
    }

    profile = router.get_call_profile("glm", "structured_json")

    assert profile == {
        "temperature": 0.2,
        "max_tokens": 6000,
        "timeout": 90.0,
        "max_retries": 1,
        "invoke_kwargs": {
            "thinking": {"type": "auto"},
            "response_format": {"type": "json_object"},
        },
    }


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
    orch.config = {
        "llm": {"default": "planner", "models": {"planner": {}}},
        "task_routing": {"planner_model": "planner"},
    }
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
    orch.config = {
        "llm": {"default": "planner", "models": {"planner": {}}},
        "task_routing": {"planner_model": "planner"},
    }
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
        "doubao-pro": {"aliases": ["doubao", "豆包"]},
    }}}

    assert agent._extract_model_override(
        "用 gemini flash 来清理空间"
    ) == "gemini-flash"
    assert agent._extract_model_override(
        "搜索产品 [model=gemini-pro]"
    ) == "gemini-pro"
    assert agent._extract_model_override(
        "用 doubao 查一下电动自行车"
    ) == "doubao-pro"
    assert agent._extract_model_override(
        "使用豆包来查账单"
    ) == "doubao-pro"
    assert agent._extract_model_override("普通聊天") is None


def test_unknown_bracket_model_is_rejected():
    agent = Agent.__new__(Agent)
    agent._config = {"llm": {"models": {"flash": {}}}}

    import pytest
    with pytest.raises(ValueError, match="未配置模型"):
        agent._extract_model_override("执行任务 [model=not-real]")


def test_hard_model_policy_controls_planner_and_worker_route():
    config = {
        "llm": {
            "default": "glm",
            "models": {"glm": {}, "doubao-pro": {}, "flash": {}},
        },
        "task_routing": {
            "planner_model": "glm",
            "route_rules": [{
                "type": "text", "model": "glm", "fallback": "flash",
                "allowed_models": ["glm", "doubao-pro", "flash"],
            }],
        },
    }
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.dag_max_steps = 30
    orch.planner_model = "glm"
    orch.config = config
    orch.model_selector = ModelSelector(config)
    orch.router = MagicMock()
    invoker = MagicMock(model_name="doubao-actual")
    invoker.invoke_sync.return_value = {
        "content": json.dumps({"global_strategy": "", "subtasks": []}),
        "finish_reason": "stop", "usage_total": 1,
    }
    orch.router.get_invoker.return_value = invoker
    orch.router.get_driver.return_value = "ark"
    orch.router.get_call_profile.return_value = {
        "temperature": 0.2, "max_tokens": 8192, "timeout": 90.0,
        "max_retries": 2,
        "invoke_kwargs": {"thinking": {"type": "disabled"}},
    }
    orch.request_selector = MagicMock()
    orch.request_selector.select.return_value = SimpleNamespace(
        names=[], confidence="high"
    )
    orch.skill_engine = MagicMock()
    orch.skill_engine.get_all_names.return_value = set()
    policy = ExecutionPolicy.user_locked("doubao-pro")

    orch._plan("research", execution_policy=policy)
    subtask = Subtask(id="s1", name="research")
    orch._classify_and_route([subtask], execution_policy=policy)

    assert orch.router.get_invoker.call_args_list[0].args[0] == "doubao-pro"
    assert subtask.assigned_model == "doubao-pro"
    assert subtask.model_reason == "user:hard"
    assert subtask.fallback_models == []


def test_worker_factory_reuses_router_invoker():
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.router = MagicMock()
    orch.router.models_cfg = {
        "doubao-pro": {"model": "doubao-actual", "max_steps": 8},
    }
    invoker = MagicMock(model_name="doubao-actual")
    orch.router.get_invoker.return_value = invoker
    orch.router.get_driver.return_value = "ark"
    orch.router.get_call_profile.return_value = {
        "temperature": 0.2, "max_tokens": 8192, "timeout": 90.0,
        "max_retries": 2,
        "invoke_kwargs": {"thinking": {"type": "disabled"}},
    }
    orch.skill_engine = MagicMock()
    orch.ledger = None
    orch._active_worker_max_steps = 5
    orch._active_token_budget = 1000
    subtask = Subtask(id="s1", name="research", assigned_model="doubao-pro")

    worker = orch._make_worker(subtask, "doubao-pro")

    assert worker.model_invoker is invoker
    assert worker.max_steps == 5
    orch.router.get_invoker.assert_called_once_with(
        "doubao-pro", profile="tool_loop"
    )
    assert worker.max_retries == 2
    assert worker.call_timeout == 90.0
    assert worker.call_kwargs == {"thinking": {"type": "disabled"}}


def test_worker_model_error_uses_only_decision_fallbacks():
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    primary = MagicMock()
    primary.run.return_value = WorkerOutcome(
        "primary failed", status="failed", terminal_reason="model_error"
    )
    fallback = MagicMock()
    fallback.run.return_value = WorkerOutcome("fallback ok")
    orch._make_worker = MagicMock(side_effect=[primary, fallback])
    orch._log_and_persist = MagicMock()
    orch.direct_tool_execution = False
    subtask = Subtask(
        id="s1", name="research", assigned_model="glm",
        fallback_models=["flash"],
    )
    results = {}

    orch._run_single_subtask(
        subtask, {}, results, threading.Lock(), parent_execution_id="parent"
    )

    assert results["s1"]["status"] == "done"
    assert results["s1"]["result"] == "fallback ok"
    assert [call.args[1] for call in orch._make_worker.call_args_list] == [
        "glm", "flash",
    ]


def test_hard_worker_model_error_has_no_hidden_fallback():
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    worker = MagicMock()
    worker.run.return_value = WorkerOutcome(
        "locked failed", status="failed", terminal_reason="model_error"
    )
    orch._make_worker = MagicMock(return_value=worker)
    orch._log_and_persist = MagicMock()
    orch.direct_tool_execution = False
    subtask = Subtask(
        id="s1", name="research", assigned_model="doubao-pro",
        fallback_models=[],
    )
    results = {}

    orch._run_single_subtask(
        subtask, {}, results, threading.Lock(), parent_execution_id="parent"
    )

    assert results["s1"]["status"] == "failed"
    assert "locked failed" in results["s1"]["error"]
    orch._make_worker.assert_called_once()


def test_unavailable_primary_worker_uses_declared_fallback():
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    fallback = MagicMock()
    fallback.run.return_value = WorkerOutcome("fallback ok")
    orch._make_worker = MagicMock(side_effect=[
        RuntimeError("primary unavailable"), fallback,
    ])
    orch._log_and_persist = MagicMock()
    orch.direct_tool_execution = False
    subtask = Subtask(
        id="s1", name="research", assigned_model="glm",
        fallback_models=["flash"],
    )
    results = {}

    orch._run_single_subtask(
        subtask, {}, results, threading.Lock(),
        parent_execution_id="parent", worker_max_steps=5,
        worker_token_budget=1000,
    )

    assert results["s1"]["status"] == "done"
    assert results["s1"]["result"] == "fallback ok"
    assert [call.args[1] for call in orch._make_worker.call_args_list] == [
        "glm", "flash",
    ]


def test_worker_outcome_with_tools_blocks_fallback_from_empty_subtask():
    """T1: 验证从空 Subtask 起步，若 WorkerOutcome 包含工具调用结果，严禁自动调用 fallback"""
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    primary = MagicMock()
    # 真实情况：Subtask 初始无 tool_results，Worker 内部产生工具结果后返回在 WorkerOutcome 中
    primary.run.return_value = WorkerOutcome(
        "model error after tool", status="failed", terminal_reason="model_error",
        tool_results=[{"name": "write_file", "result": "done"}]
    )
    fallback = MagicMock()
    orch._make_worker = MagicMock(side_effect=[primary, fallback])
    orch._log_and_persist = MagicMock()
    orch.direct_tool_execution = False
    subtask = Subtask(
        id="s1", name="mutation_task", assigned_model="glm",
        fallback_models=["flash"],
        tool_results=[]  # 初始为空！
    )
    results = {}

    orch._run_single_subtask(
        subtask, {}, results, threading.Lock(), parent_execution_id="parent"
    )

    assert results["s1"]["status"] == "failed"
    # 确认仅创建了主 worker，备用 worker 未被创建/运行
    assert [call.args[1] for call in orch._make_worker.call_args_list] == ["glm"]
    log_messages = [call.args[0] for call in orch._log_and_persist.call_args_list]
    assert any("具有副作用，终止自动回退重跑" in msg for msg in log_messages)


def test_worker_exception_after_execution_blocks_fallback():
    """T1: 验证当 Worker 执行期间（已走步骤）抛出异常时，严禁盲目回退重跑"""
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    primary = MagicMock()
    def fake_run(subtask, *args, **kwargs):
        subtask.steps_used = 1  # 模拟执行已经开始走步
        raise RuntimeError("network dropped during tool execution")
    primary.run.side_effect = fake_run
    orch._make_worker = MagicMock(return_value=primary)
    orch._log_and_persist = MagicMock()
    orch.direct_tool_execution = False
    subtask = Subtask(
        id="s1", name="mutation_task", assigned_model="glm",
        fallback_models=["flash"],
        tool_results=[]
    )
    results = {}

    orch._run_single_subtask(
        subtask, {}, results, threading.Lock(), parent_execution_id="parent"
    )

    assert results["s1"]["status"] == "failed"
    assert [call.args[1] for call in orch._make_worker.call_args_list] == ["glm"]
    log_messages = [call.args[0] for call in orch._log_and_persist.call_args_list]
    assert any("终止自动回退" in msg for msg in log_messages)


def test_fallback_chain_stops_when_fallback_executes_tools():
    """T1: 验证备用模型链中，若第一备用执行了工具但失败，严禁继续调用第二备用"""
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    primary = MagicMock()
    # 主模型未执行工具直接报错（可安全回退到 fallback1）
    primary.run.return_value = WorkerOutcome(
        "primary connection error", status="failed", terminal_reason="model_error",
        tool_results=[]
    )
    # 第一备用模型执行了工具后发生 model_error
    fb1 = MagicMock()
    fb1.run.return_value = WorkerOutcome(
        "fb1 model error after tool", status="failed", terminal_reason="model_error",
        tool_results=[{"name": "charge_card", "result": "ok"}]
    )
    fb2 = MagicMock()
    fb2.run.return_value = WorkerOutcome("fb2 ok")
    orch._make_worker = MagicMock(side_effect=[primary, fb1, fb2])
    orch._log_and_persist = MagicMock()
    orch.direct_tool_execution = False
    subtask = Subtask(
        id="s1", name="charge_task", assigned_model="glm",
        fallback_models=["fb1", "fb2"],
        tool_results=[]
    )
    results = {}

    orch._run_single_subtask(
        subtask, {}, results, threading.Lock(), parent_execution_id="parent"
    )

    assert results["s1"]["status"] == "failed"
    # 关键断言：仅创建了 glm 与 fb1，fb2 坚决未被创建或执行！
    assert [call.args[1] for call in orch._make_worker.call_args_list] == ["glm", "fb1"]
    log_messages = [call.args[0] for call in orch._log_and_persist.call_args_list]
    assert any("终止后续备用回退" in msg for msg in log_messages)

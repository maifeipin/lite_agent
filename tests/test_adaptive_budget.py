import json

from core.adaptive_budget import (
    AdaptiveBudgetPolicy,
    AdaptiveStepController,
    BudgetAction,
    EvidenceBoard,
    compact_messages,
)
from core.agent_runtime import AgentRuntime, RuntimeEventType
from core.execution import (
    ActorType,
    ExecutionContext,
    ExecutionResult,
    ExecutionSource,
)


def _policy(**overrides):
    values = {
        "enabled": True,
        "initial_steps": 3,
        "lease_steps": 3,
        "finalization_reserve": 1,
        "low_yield_streak": 2,
        "low_result_chars": 80,
        "max_context_chars": 4000,
        "recent_tool_rounds": 1,
    }
    values.update(overrides)
    return AdaptiveBudgetPolicy(**values)


def test_config_requires_explicit_enable():
    assert AdaptiveBudgetPolicy.from_config({}).enabled is False
    configured = AdaptiveBudgetPolicy.from_config({
        "adaptive_execution": {"enabled": True, "simple_hard_steps": 50}
    })
    assert configured.enabled is True
    assert configured.simple_hard_steps == 50


def test_high_gain_extends_lease_without_changing_hard_limit():
    controller = AdaptiveStepController(10, _policy(initial_steps=2), goal="研究")
    assert controller.before_step(1).action == BudgetAction.EXECUTE
    controller.record_tool_result(
        step=1, tool_name="gaokao_sql", arguments="{}",
        ok=True, output="权威结构化数据",
    )
    assert controller.before_step(2).action == BudgetAction.EXECUTE
    decision = controller.before_step(3)
    assert decision.action == BudgetAction.EXTEND
    assert decision.lease_limit == 5
    assert controller.hard_steps == 10


def test_low_yield_replans_once_then_finalizes():
    controller = AdaptiveStepController(10, _policy(initial_steps=6), goal="检索")
    for step in (1, 2):
        controller.record_tool_result(
            step=step, tool_name="web_search",
            arguments=json.dumps({"q": f"query-{step}"}),
            ok=True, output="无相关结果",
        )
    assert controller.before_step(3).action == BudgetAction.REPLAN

    for step in (3, 4, 5, 6):
        controller.record_tool_result(
            step=step, tool_name="web_search",
            arguments=json.dumps({"q": f"other-{step}"}),
            ok=True, output="仍无结果",
        )
    decision = controller.before_step(7)
    assert decision.action == BudgetAction.FINALIZE
    assert "不得再调用任何工具" in decision.instruction


def test_high_gain_during_replan_lease_rescues_useful_work():
    controller = AdaptiveStepController(12, _policy(initial_steps=3), goal="研究")
    for step in (1, 2):
        controller.record_tool_result(
            step=step, tool_name="web_search",
            arguments=json.dumps({"q": f"q-{step}"}),
            ok=True, output="无结果",
        )
    replan = controller.before_step(3)
    assert replan.action == BudgetAction.REPLAN

    controller.record_tool_result(
        step=3, tool_name="web_search", arguments='{"q":"new-route"}',
        ok=True, output="仍无结果",
    )
    assert controller.before_step(4).action == BudgetAction.EXECUTE
    controller.record_tool_result(
        step=4, tool_name="gaokao_sql", arguments="{}",
        ok=True, output="新的权威结构化证据",
    )
    assert controller.before_step(5).action == BudgetAction.EXECUTE
    assert controller.before_step(6).action == BudgetAction.EXTEND


def test_last_step_is_reserved_for_finalization():
    controller = AdaptiveStepController(4, _policy(initial_steps=3), goal="任务")
    assert [controller.before_step(step).action for step in (1, 2, 3)] == [
        BudgetAction.EXECUTE,
        BudgetAction.EXECUTE,
        BudgetAction.EXECUTE,
    ]
    assert controller.before_step(4).action == BudgetAction.FINALIZE


def test_duplicate_url_is_low_gain_after_tracking_parameters_removed():
    board = EvidenceBoard(low_result_chars=10)
    first = board.record(
        step=1, tool_name="ops_web_fetch",
        arguments='{"url":"https://example.com/a?utm_source=x&id=1"}',
        ok=True, output="一段足够长而且包含有效事实的新内容",
    )
    second = board.record(
        step=2, tool_name="ops_web_fetch",
        arguments='{"url":"https://example.com/a?id=1&utm_source=y"}',
        ok=True, output="另一段内容",
    )
    assert first.gain != "low"
    assert second.reason == "duplicate_result"


def test_compaction_keeps_protocol_pair_and_does_not_mutate_raw_history():
    board = EvidenceBoard()
    board.record(
        step=1, tool_name="gaokao_sql", arguments="{}",
        ok=True, output="已确认事实",
    )
    huge = "x" * 12000
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "goal"},
        {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "web_search", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": huge},
    ]

    compacted = compact_messages(
        messages, base_message_count=2, board=board,
        max_chars=4000, recent_tool_rounds=1,
    )

    assert len(messages[-1]["content"]) == 12000
    assert compacted[-2]["tool_calls"][0]["id"] == "c1"
    assert compacted[-1]["tool_call_id"] == "c1"
    assert "原始内容未重复注入" in compacted[-1]["content"]
    assert any("执行证据看板" in item.get("content", "") for item in compacted)


def test_runtime_replans_and_returns_summary_instead_of_max_steps():
    calls = []

    class Invoker:
        model_name = "test-model"

        def invoke_sync(self, messages, tools=None, **kwargs):
            calls.append((messages, tools))
            if not tools:
                return {
                    "content": "基于现有证据的最终总结",
                    "tool_calls": [], "usage_total": 1,
                    "finish_reason": "stop", "empty": False,
                }
            index = len(calls)
            return {
                "content": "",
                "tool_calls": [{
                    "id": f"c{index}", "name": "web_search",
                    "arguments": json.dumps({"q": f"query-{index}"}),
                }],
                "usage_total": 1, "finish_reason": "tool_calls",
                "empty": False,
            }

    class SkillEngine:
        def execute_with_context(self, ctx, name, arguments):
            return ExecutionResult(ok=True, output="无结果", tool_name=name)

    ctx = ExecutionContext(
        actor_id="u", actor_type=ActorType.USER,
        source=ExecutionSource.STREAM, max_steps=8,
        max_output_tokens=1024,
    )
    runtime = AgentRuntime(
        Invoker(), SkillEngine(), max_steps=8, max_tokens=1024,
        adaptive_policy=_policy(initial_steps=6),
    )
    events = list(runtime.run(
        [{"role": "user", "content": "研究这个问题"}],
        [{"type": "function", "function": {"name": "web_search"}}],
        ctx, stream=False,
    ))

    actions = [
        event.data["action"] for event in events
        if event.type == RuntimeEventType.BUDGET_DECISION
    ]
    assert actions == ["replan", "finalize"]
    assert RuntimeEventType.MAX_STEPS not in [event.type for event in events]
    done = [event for event in events if event.type == RuntimeEventType.DONE][-1]
    assert done.data["content"] == "基于现有证据的最终总结"
    assert calls[-1][1] == []

"""Phase 2c - AgentRuntime 独立测试

覆盖：
  - 纯文本回复（无工具调用）
  - 工具调用 -> 工具结果 -> 最终回复（多步循环）
  - 多工具并行调用
  - 死循环熔断
  - max_steps 熔断
  - 模型调用异常
  - 权限拒绝 -> 始终调用 execute_with_context
  - 受限工具不传给模型
  - 同步模式（stream=False，模拟 Gemini/Worker）
  - 两次 run() 的死循环计数互不污染
  - Context 的步数、Token 预算真实生效
  - 事件类型完备性 + 不可变性
  - Runtime 不持有 Session/Memory/Channel
"""

import pytest
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from core.agent_runtime import AgentRuntime, RuntimeEvent, RuntimeEventType
from core.execution import ExecutionContext, ExecutionResult, ExecutionSource, ActorType
from core.model_event import ModelEvent, ModelEventType
from core.model_invoker import OpenAIInvoker


# ================================================================
#  辅助函数
# ================================================================

def _make_ctx(allowed_tools=None, is_guest=False, max_steps=8,
              max_output_tokens=2048, token_budget=None):
    """构造 ExecutionContext。"""
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


def _make_stream_events(text=None, tool_calls=None, usage=None, finish_reason="stop"):
    """构造 ModelEvent 流。"""
    events = []
    if text:
        events.append(ModelEvent(type=ModelEventType.TEXT, data=text))
    if tool_calls:
        for i, tc in enumerate(tool_calls):
            events.append(ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
                "index": i, "id": tc["id"], "name": tc["name"], "arguments": tc["arguments"],
            }))
    if usage:
        events.append(ModelEvent(type=ModelEventType.USAGE, data=usage))
    events.append(ModelEvent(type=ModelEventType.DONE, data={"finish_reason": finish_reason}))
    return events


def _make_sync_result(content="", tool_calls=None, usage_total=0, finish_reason="stop"):
    """构造 invoke_sync 返回的 dict。"""
    return {
        "content": content,
        "tool_calls": tool_calls or [],
        "finish_reason": finish_reason,
        "usage_total": usage_total,
        "empty": False,
    }


# ================================================================
#  纯文本回复
# ================================================================

class TestTextReply:
    """无工具调用的纯文本回复。"""

    def test_text_reply_emits_done(self):
        """模型返回纯文本 -> 产出 TEXT + STEP_END + DONE。"""
        invoker = MagicMock()
        invoker.invoke_stream.return_value = iter(_make_stream_events(
            text="Hello world",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        ))
        se = MagicMock()

        runtime = AgentRuntime(invoker, se, max_steps=1)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            ctx=_make_ctx(),
        ))

        types = [e.type for e in events]
        assert RuntimeEventType.STEP_START in types
        assert RuntimeEventType.TEXT in types
        assert RuntimeEventType.STEP_END in types
        assert RuntimeEventType.DONE in types

        done = [e for e in events if e.type == RuntimeEventType.DONE][0]
        assert done.data["content"] == "Hello world"
        assert done.data["usage_total"] == 15

    def test_empty_reply_fallback(self):
        """模型返回空文本 -> DONE 携带空 content + finish_reason，由调用方决定兜底格式。"""
        invoker = MagicMock()
        invoker.invoke_stream.return_value = iter(_make_stream_events(
            text="", finish_reason="length",
        ))
        se = MagicMock()

        runtime = AgentRuntime(invoker, se, max_steps=1)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            ctx=_make_ctx(),
        ))

        done = [e for e in events if e.type == RuntimeEventType.DONE][0]
        # Runtime 不再生成兜底提示，仅传递空 content 与 finish_reason
        assert done.data["content"] == ""
        assert done.data["finish_reason"] == "length"
        assert done.data["empty"] is False

    def test_output_limit_retries_once_with_configured_kwargs(self):
        invoker = MagicMock()
        invoker.output_limit_retry_kwargs.return_value = {
            "extra_body": {"enable_thinking": False}
        }
        invoker.invoke_stream.side_effect = [
            iter(_make_stream_events(text=None, finish_reason="length")),
            iter(_make_stream_events(text="完整答案", finish_reason="stop")),
        ]
        runtime = AgentRuntime(invoker, MagicMock(), max_steps=3, max_tokens=8192)

        events = list(runtime.run(
            messages=[{"role": "user", "content": "优化行程"}],
            tools=[], ctx=_make_ctx(max_steps=3, max_output_tokens=8192),
        ))

        done = [e for e in events if e.type == RuntimeEventType.DONE][-1]
        assert done.data["content"] == "完整答案"
        assert invoker.invoke_stream.call_count == 2
        assert invoker.invoke_stream.call_args_list[1].kwargs["extra_body"] == {
            "enable_thinking": False
        }

    def test_profile_call_kwargs_are_applied_to_every_model_step(self):
        invoker = MagicMock()
        invoker.invoke_sync.return_value = _make_sync_result(content="ok")
        runtime = AgentRuntime(
            invoker, MagicMock(), max_steps=1,
            call_kwargs={"thinking": {"type": "disabled"}},
        )

        list(runtime.run(
            messages=[{"role": "user", "content": "hi"}],
            tools=[], ctx=_make_ctx(), stream=False,
        ))

        assert invoker.invoke_sync.call_args.kwargs["thinking"] == {
            "type": "disabled"
        }

    def test_output_limit_continuation_keeps_partial_text(self):
        invoker = MagicMock()
        invoker.output_limit_retry_kwargs.return_value = {
            "thinking": {"type": "disabled"}
        }
        invoker.invoke_stream.side_effect = [
            iter(_make_stream_events(text="前半段。", finish_reason="length")),
            iter(_make_stream_events(text="后半段。", finish_reason="stop")),
        ]
        runtime = AgentRuntime(invoker, MagicMock(), max_steps=3, max_tokens=8192)

        events = list(runtime.run(
            messages=[{"role": "user", "content": "长报告"}],
            tools=[], ctx=_make_ctx(max_steps=3, max_output_tokens=8192),
        ))

        done = [e for e in events if e.type == RuntimeEventType.DONE][-1]
        assert done.data["content"] == "前半段。后半段。"
        second_messages = invoker.invoke_stream.call_args_list[1].args[0]
        assert second_messages[-3]["content"] == "前半段。"
        assert "不要重复前文" in second_messages[-2]["content"]


# ================================================================
#  工具调用循环
# ================================================================

class TestToolCallLoop:
    """工具调用 -> 工具结果 -> 最终回复。"""

    def test_single_tool_then_reply(self):
        """单工具调用后第二轮返回文本。"""
        invoker = MagicMock()
        call_count = [0]

        def fake_stream(messages, tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                yield from _make_stream_events(tool_calls=[
                    {"id": "call_1", "name": "echo", "arguments": '{"text":"hi"}'},
                ], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
            else:
                yield from _make_stream_events(text="Done!", usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30})

        invoker.invoke_stream = fake_stream
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="echo: hi", tool_name="echo", tool_call_id="call_1",
        )

        runtime = AgentRuntime(invoker, se, max_steps=5)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "echo hi"}],
            tools=[{"type": "function", "function": {"name": "echo"}}],
            ctx=_make_ctx(),
        ))

        types = [e.type for e in events]
        assert RuntimeEventType.TOOL_CALL in types
        assert RuntimeEventType.TOOL_RESULT in types
        assert RuntimeEventType.DONE in types

        tool_result = [e for e in events if e.type == RuntimeEventType.TOOL_RESULT][0]
        assert tool_result.data["ok"] is True
        assert tool_result.data["output"] == "echo: hi"

        done = [e for e in events if e.type == RuntimeEventType.DONE][0]
        assert done.data["content"] == "Done!"
        assert done.data["usage_total"] == 45

    def test_multi_tool_parallel(self):
        """两个工具并行调用。"""
        invoker = MagicMock()
        call_count = [0]

        def fake_stream(messages, tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                yield from _make_stream_events(tool_calls=[
                    {"id": "call_a", "name": "search", "arguments": '{"q":"x"}'},
                    {"id": "call_b", "name": "fetch", "arguments": '{"url":"y"}'},
                ])
            else:
                yield from _make_stream_events(text="All done")

        invoker.invoke_stream = fake_stream
        se = MagicMock()
        se.execute_with_context.side_effect = [
            ExecutionResult(ok=True, output="result_a", tool_name="search"),
            ExecutionResult(ok=True, output="result_b", tool_name="fetch"),
        ]

        runtime = AgentRuntime(invoker, se, max_steps=3)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "do"}],
            tools=[],
            ctx=_make_ctx(),
        ))

        tool_calls = [e for e in events if e.type == RuntimeEventType.TOOL_CALL]
        tool_results = [e for e in events if e.type == RuntimeEventType.TOOL_RESULT]
        assert len(tool_calls) == 2
        assert len(tool_results) == 2
        assert tool_results[0].data["output"] == "result_a"
        assert tool_results[1].data["output"] == "result_b"

    def test_tool_exception_handled(self):
        """工具执行抛异常 -> ok=False, output 包含错误信息。"""
        invoker = MagicMock()
        invoker.invoke_stream.return_value = iter(_make_stream_events(tool_calls=[
            {"id": "call_1", "name": "boom", "arguments": "{}"},
        ]))

        se = MagicMock()
        se.execute_with_context.side_effect = RuntimeError("boom error")

        runtime = AgentRuntime(invoker, se, max_steps=3)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            ctx=_make_ctx(),
        ))

        tool_results = [e for e in events if e.type == RuntimeEventType.TOOL_RESULT]
        assert len(tool_results) == 1
        assert tool_results[0].data["ok"] is False
        assert "boom error" in tool_results[0].data["output"]


# ================================================================
#  熔断
# ================================================================

class TestCircuitBreakers:
    """死循环检测和 max_steps 熔断。"""

    def test_dead_loop_triggers(self):
        """连续 3 次相同工具+参数 -> DEAD_LOOP 事件。"""
        invoker = MagicMock()
        call_count = [0]

        def fake_stream(messages, tools=None, **kwargs):
            call_count[0] += 1
            yield from _make_stream_events(tool_calls=[
                {"id": f"call_{call_count[0]}", "name": "echo", "arguments": '{"text":"a"}'},
            ])

        invoker.invoke_stream = fake_stream
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="a", tool_name="echo",
        )

        runtime = AgentRuntime(invoker, se, max_steps=10)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            ctx=_make_ctx(),
        ))

        types = [e.type for e in events]
        assert RuntimeEventType.DEAD_LOOP in types
        dead_loop_idx = types.index(RuntimeEventType.DEAD_LOOP)
        assert types[dead_loop_idx + 1] == RuntimeEventType.DONE

    def test_max_steps_triggers(self):
        """超出 max_steps -> MAX_STEPS 事件。"""
        invoker = MagicMock()

        def fake_stream(messages, tools=None, **kwargs):
            import json
            yield from _make_stream_events(tool_calls=[
                {"id": "call_1", "name": "echo", "arguments": json.dumps({"text": str(len(messages))})},
            ])

        invoker.invoke_stream = fake_stream
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="ok", tool_name="echo",
        )

        runtime = AgentRuntime(invoker, se, max_steps=2)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            ctx=_make_ctx(),
        ))

        types = [e.type for e in events]
        assert RuntimeEventType.MAX_STEPS in types
        assert RuntimeEventType.DONE in types

    def test_context_max_steps_overrides_runtime(self):
        """ctx.max_steps < runtime.max_steps -> 以 ctx 为准。"""
        invoker = MagicMock()

        def fake_stream(messages, tools=None, **kwargs):
            import json
            yield from _make_stream_events(tool_calls=[
                {"id": "call_1", "name": "echo", "arguments": json.dumps({"text": str(len(messages))})},
            ])

        invoker.invoke_stream = fake_stream
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="ok", tool_name="echo",
        )

        runtime = AgentRuntime(invoker, se, max_steps=10)
        # ctx 限制 max_steps=2，runtime 允许 10
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            ctx=_make_ctx(max_steps=2),
        ))

        # 只跑了 2 步就 MAX_STEPS
        step_starts = [e for e in events if e.type == RuntimeEventType.STEP_START]
        assert len(step_starts) == 2
        assert any(e.type == RuntimeEventType.MAX_STEPS for e in events)

    def test_token_budget_exceeded(self):
        """token_budget 耗尽 -> TOKEN_BUDGET_EXCEEDED 事件。"""
        invoker = MagicMock()
        call_count = [0]

        def fake_stream(messages, tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一步返回 tool_call 消耗 100 tokens
                yield from _make_stream_events(
                    tool_calls=[{"id": "c1", "name": "echo", "arguments": '{}'}],
                    usage={"prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100},
                )
            else:
                # 第二步如果被执行，返回文本
                yield from _make_stream_events(text="response")

        invoker.invoke_stream = fake_stream
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="ok", tool_name="echo",
        )

        runtime = AgentRuntime(invoker, se, max_steps=5)
        # 第一步消耗 100 tokens，budget=50 -> 第二步开始时触发预算熔断
        events = list(runtime.run(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            ctx=_make_ctx(token_budget=50),
        ))

        types = [e.type for e in events]
        assert RuntimeEventType.TOKEN_BUDGET_EXCEEDED in types
        assert RuntimeEventType.DONE in types

        budget_event = [e for e in events if e.type == RuntimeEventType.TOKEN_BUDGET_EXCEEDED][0]
        assert budget_event.data["budget"] == 50
        assert budget_event.data["used"] == 100


# ================================================================
#  模型调用异常
# ================================================================

class TestModelError:
    """模型调用异常处理。"""

    def test_stream_error_yields_error_event(self):
        """invoke_stream 抛异常 -> ERROR + DONE。"""
        invoker = MagicMock()

        def fake_stream(messages, tools=None, **kwargs):
            raise RuntimeError("connection failed")
            yield  # 使其成为 generator

        invoker.invoke_stream = fake_stream
        se = MagicMock()

        runtime = AgentRuntime(invoker, se, max_steps=3)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            ctx=_make_ctx(),
        ))

        types = [e.type for e in events]
        assert RuntimeEventType.ERROR in types
        assert RuntimeEventType.DONE in types

        error = [e for e in events if e.type == RuntimeEventType.ERROR][0]
        assert "connection failed" in error.data["msg"]


# ================================================================
#  权限控制 - 工具过滤 + 始终调用 execute_with_context
# ================================================================

class TestPermissionControl:
    """通过 ExecutionContext 控制工具访问权限。"""

    def test_restricted_tool_not_sent_to_model(self):
        """allowed_tools 不包含的工具 -> 不传给模型。"""
        invoker = MagicMock()
        invoker.invoke_stream.return_value = iter(_make_stream_events(text="ok"))
        se = MagicMock()

        runtime = AgentRuntime(invoker, se, max_steps=3)
        ctx = _make_ctx(allowed_tools=["allowed_tool"])

        tools = [
            {"type": "function", "function": {"name": "allowed_tool"}},
            {"type": "function", "function": {"name": "forbidden_tool"}},
        ]
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=tools,
            ctx=ctx,
        ))

        # 验证传给 invoke_stream 的 tools 不含 forbidden_tool
        call_args = invoker.invoke_stream.call_args
        passed_tools = call_args[0][1]  # 第二个位置参数
        assert len(passed_tools) == 1
        assert passed_tools[0]["function"]["name"] == "allowed_tool"

    def test_allowed_tools_none_passes_all(self):
        """allowed_tools=None -> 全部工具传给模型。"""
        invoker = MagicMock()
        invoker.invoke_stream.return_value = iter(_make_stream_events(text="ok"))
        se = MagicMock()

        runtime = AgentRuntime(invoker, se, max_steps=3)
        ctx = _make_ctx(allowed_tools=None)

        tools = [
            {"type": "function", "function": {"name": "a"}},
            {"type": "function", "function": {"name": "b"}},
        ]
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=tools,
            ctx=ctx,
        ))

        call_args = invoker.invoke_stream.call_args
        passed_tools = call_args[0][1]
        assert len(passed_tools) == 2

    def test_empty_allowed_tools_passes_no_tools(self):
        """allowed_tools=[] -> 不传任何工具给模型。"""
        invoker = MagicMock()
        invoker.invoke_stream.return_value = iter(_make_stream_events(text="ok"))
        se = MagicMock()

        runtime = AgentRuntime(invoker, se, max_steps=3)
        ctx = _make_ctx(allowed_tools=[])

        tools = [
            {"type": "function", "function": {"name": "a"}},
        ]
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=tools,
            ctx=ctx,
        ))

        call_args = invoker.invoke_stream.call_args
        passed_tools = call_args[0][1]
        assert passed_tools == []

    def test_permission_denied_calls_execute_with_context(self):
        """权限拒绝时仍调用 execute_with_context，由执行网关返回结构化拒绝。"""
        invoker = MagicMock()
        invoker.invoke_stream.return_value = iter(_make_stream_events(tool_calls=[
            {"id": "call_1", "name": "forbidden_tool", "arguments": "{}"},
        ]))
        se = MagicMock()
        # execute_with_context 返回拒绝结果
        se.execute_with_context.return_value = ExecutionResult(
            ok=False, output="❌ 权限不足：无权调用工具 forbidden_tool",
            tool_name="forbidden_tool", error_code="PERMISSION_DENIED",
        )

        runtime = AgentRuntime(invoker, se, max_steps=3)
        ctx = _make_ctx(allowed_tools=None)  # 不过滤 tools，但 execute 会拒绝
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "forbidden_tool"}}],
            ctx=ctx,
        ))

        # execute_with_context 必须被调用
        se.execute_with_context.assert_called_once()
        tool_results = [e for e in events if e.type == RuntimeEventType.TOOL_RESULT]
        assert len(tool_results) == 1
        assert tool_results[0].data["ok"] is False
        assert "权限不足" in tool_results[0].data["output"]


# ================================================================
#  同步模式（Gemini/Worker）
# ================================================================

class TestSyncMode:
    """stream=False 同步模式测试。"""

    def test_sync_text_reply(self):
        """同步模式纯文本回复。"""
        invoker = MagicMock()
        invoker.invoke_sync.return_value = _make_sync_result(
            content="Hello from sync", usage_total=20,
        )
        se = MagicMock()

        runtime = AgentRuntime(invoker, se, max_steps=3)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            ctx=_make_ctx(),
            stream=False,
        ))

        types = [e.type for e in events]
        assert RuntimeEventType.TEXT in types
        assert RuntimeEventType.DONE in types
        assert RuntimeEventType.USAGE in types

        done = [e for e in events if e.type == RuntimeEventType.DONE][0]
        assert done.data["content"] == "Hello from sync"
        assert done.data["usage_total"] == 20

    def test_sync_tool_call_then_reply(self):
        """同步模式工具调用循环。"""
        invoker = MagicMock()
        call_count = [0]

        def fake_sync(messages, tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_sync_result(tool_calls=[
                    {"id": "call_1", "name": "echo", "arguments": '{"text":"hi"}'},
                ], usage_total=15)
            else:
                return _make_sync_result(content="Sync done!", usage_total=30)

        invoker.invoke_sync.side_effect = fake_sync
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="echo: hi", tool_name="echo",
        )

        runtime = AgentRuntime(invoker, se, max_steps=5)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "echo hi"}],
            tools=[{"type": "function", "function": {"name": "echo"}}],
            ctx=_make_ctx(),
            stream=False,
        ))

        types = [e.type for e in events]
        assert RuntimeEventType.TOOL_CALL in types
        assert RuntimeEventType.TOOL_RESULT in types
        assert RuntimeEventType.DONE in types

        done = [e for e in events if e.type == RuntimeEventType.DONE][0]
        assert done.data["content"] == "Sync done!"

    def test_sync_gemini_completes_tool_loop(self):
        """模拟 Gemini 同步模式完成完整工具循环。"""
        invoker = MagicMock()
        call_count = [0]

        def fake_sync(messages, tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_sync_result(tool_calls=[
                    {"id": "call_1", "name": "search", "arguments": '{"q":"test"}'},
                ], usage_total=10)
            else:
                return _make_sync_result(content="Gemini result", usage_total=20)

        invoker.invoke_sync.side_effect = fake_sync
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="found: test", tool_name="search",
        )

        runtime = AgentRuntime(invoker, se, max_steps=5)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "search test"}],
            tools=[{"type": "function", "function": {"name": "search"}}],
            ctx=_make_ctx(),
            stream=False,  # Gemini 同步模式
        ))

        # 验证 invoke_sync 被调用（不是 invoke_stream）
        assert invoker.invoke_sync.call_count == 2
        assert invoker.invoke_stream.call_count == 0

        done = [e for e in events if e.type == RuntimeEventType.DONE][0]
        assert done.data["content"] == "Gemini result"


# ================================================================
#  LoopDetector 隔离
# ================================================================

class TestLoopDetectorIsolation:
    """两次 run() 的死循环计数互不污染。"""

    def test_two_runs_streak_isolated(self):
        """同一 Runtime 实例连续两次 run()，前一次的 streak 不影响后一次。"""
        invoker = MagicMock()
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="ok", tool_name="echo",
        )

        runtime = AgentRuntime(invoker, se, max_steps=10)

        # 第一次 run()：调用 echo 2 次（不触发，streak=2）
        call_count_1 = [0]

        def fake_stream_1(messages, tools=None, **kwargs):
            call_count_1[0] += 1
            if call_count_1[0] <= 2:
                yield from _make_stream_events(tool_calls=[
                    {"id": f"c{call_count_1[0]}", "name": "echo", "arguments": '{"text":"a"}'},
                ])
            else:
                yield from _make_stream_events(text="done")

        invoker.invoke_stream = fake_stream_1
        events_1 = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            ctx=_make_ctx(),
        ))
        # 第一次 run 不应触发死循环
        assert RuntimeEventType.DEAD_LOOP not in [e.type for e in events_1]

        # 第二次 run()：同样的 echo 参数，需要再累积 3 次才触发
        call_count_2 = [0]

        def fake_stream_2(messages, tools=None, **kwargs):
            call_count_2[0] += 1
            if call_count_2[0] <= 2:
                yield from _make_stream_events(tool_calls=[
                    {"id": f"c2_{call_count_2[0]}", "name": "echo", "arguments": '{"text":"a"}'},
                ])
            else:
                yield from _make_stream_events(text="done")

        invoker.invoke_stream = fake_stream_2
        events_2 = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            ctx=_make_ctx(),
        ))
        # 第二次 run 不应被第一次的 streak 污染
        assert RuntimeEventType.DEAD_LOOP not in [e.type for e in events_2]


# ================================================================
#  事件不可变性 + 类型枚举
# ================================================================

class TestRuntimeEventImmutability:
    """RuntimeEvent 深度不可变 + 类型枚举。"""

    def test_type_must_be_enum(self):
        with pytest.raises(TypeError):
            RuntimeEvent(type="text", data="hello")

    def test_data_dict_is_readonly(self):
        e = RuntimeEvent(type=RuntimeEventType.TEXT, data={"key": "val"})
        assert isinstance(e.data, MappingProxyType)
        with pytest.raises(TypeError):
            e.data["key"] = "changed"

    def test_data_nested_dict_readonly(self):
        e = RuntimeEvent(type=RuntimeEventType.TOOL_RESULT,
                         data={"nested": {"inner": 1}})
        assert isinstance(e.data["nested"], MappingProxyType)
        with pytest.raises(TypeError):
            e.data["nested"]["inner"] = 999

    def test_cannot_reassign_fields(self):
        e = RuntimeEvent(type=RuntimeEventType.TEXT, data="hello")
        with pytest.raises(Exception):
            e.type = RuntimeEventType.DONE
        with pytest.raises(Exception):
            e.data = "world"

    def test_all_enum_members(self):
        members = {m.name for m in RuntimeEventType}
        assert members == {
            "STEP_START", "TEXT", "REASONING", "TOOL_CALLS_READY",
            "TOOL_CALL", "TOOL_RESULT", "USAGE", "STEP_END", "DONE",
            "ERROR", "DEAD_LOOP", "MAX_STEPS", "TOKEN_BUDGET_EXCEEDED",
            "BUDGET_DECISION", "CONTEXT_COMPACTED",
        }


# ================================================================
#  Runtime 不持有 Session/Memory/Channel
# ================================================================

class TestRuntimeScope:
    """验证 Runtime 的职责边界。"""

    def test_runtime_has_no_session_attr(self):
        invoker = MagicMock()
        se = MagicMock()
        runtime = AgentRuntime(invoker, se)
        assert not hasattr(runtime, "session_mgr")
        assert not hasattr(runtime, "session")
        assert not hasattr(runtime, "memory")
        assert not hasattr(runtime, "channel")
        assert not hasattr(runtime, "dag")
        assert not hasattr(runtime, "_loop_detector")

    def test_runtime_has_required_attrs(self):
        invoker = MagicMock()
        se = MagicMock()
        runtime = AgentRuntime(invoker, se, max_steps=5, max_tokens=1024)
        assert runtime.model_invoker is invoker
        assert runtime.skill_engine is se
        assert runtime.max_steps == 5
        assert runtime.max_tokens == 1024


# ================================================================
#  P0: provider_metadata 保留
# ================================================================

class TestProviderMetadata:
    """Gemini provider_metadata（thought_signature/call_id）在 Runtime 中不丢失。"""

    def test_sync_preserves_thought_signature_in_second_round(self):
        """两轮同步 Runtime：第一轮返回带 provider_metadata 的 Gemini tool call，
        执行工具后，第二轮 invoke_sync 收到的 messages 中 assistant tool_call
        必须保留签名、ID、名称和参数。

        测试数据使用生产格式（与 gemini_response_to_unified 一致）：
        {"id": ..., "name": ..., "arguments": ..., "provider_metadata": {"thought_signature": ...}}
        """
        invoker = MagicMock()
        call_count = [0]
        captured_messages_2nd = []

        def fake_sync(messages, tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_sync_result(tool_calls=[
                    {
                        "id": "gemini_call_1",
                        "name": "search",
                        "arguments": '{"q":"x"}',
                        "provider_metadata": {
                            "thought_signature": "sig_abc123",
                        },
                    },
                ], usage_total=10)
            else:
                # 捕获第二轮收到的 messages
                captured_messages_2nd.extend(messages)
                return _make_sync_result(content="done", usage_total=5)

        invoker.invoke_sync.side_effect = fake_sync
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="ok", tool_name="search",
        )

        runtime = AgentRuntime(invoker, se, max_steps=5)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "search"}}],
            ctx=_make_ctx(),
            stream=False,
        ))

        # 验证第二轮被调用
        assert invoker.invoke_sync.call_count == 2

        # 从第二轮 messages 中找到 assistant 消息
        assistant_msgs = [m for m in captured_messages_2nd if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        assistant = assistant_msgs[0]
        assert "tool_calls" in assistant

        tc = assistant["tool_calls"][0]
        # 基本 OpenAI 字段
        assert tc["id"] == "gemini_call_1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "search"
        assert tc["function"]["arguments"] == '{"q":"x"}'

        # provider_metadata 保留
        assert "provider_metadata" in tc
        assert tc["provider_metadata"]["thought_signature"] == "sig_abc123"

    def test_sync_provider_metadata_empty_for_openai(self):
        """OpenAI 同步结果无 thought_signature -> assistant 消息无 provider_metadata 键。"""
        invoker = MagicMock()
        call_count = [0]
        captured_messages_2nd = []

        def fake_sync(messages, tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_sync_result(tool_calls=[
                    {"id": "call_1", "name": "echo", "arguments": "{}"},
                ])
            else:
                captured_messages_2nd.extend(messages)
                return _make_sync_result(content="done")

        invoker.invoke_sync.side_effect = fake_sync
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="ok", tool_name="echo",
        )

        runtime = AgentRuntime(invoker, se, max_steps=3)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            ctx=_make_ctx(),
            stream=False,
        ))

        assistant_msgs = [m for m in captured_messages_2nd if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        tc = assistant_msgs[0]["tool_calls"][0]
        # 无 provider_metadata 键
        assert "provider_metadata" not in tc


# ================================================================
#  集成测试: gemini_response_to_unified 产物喂给 Runtime
# ================================================================

class TestGeminiCodecIntegration:
    """使用真实 gemini_response_to_unified 产物驱动 Runtime，防止 fixture 漂移。"""

    def test_gemini_unified_output_drives_runtime(self):
        """构造 Gemini 假响应 -> gemini_response_to_unified -> invoke_sync 返回 ->
        Runtime 两轮循环 -> 断言 provider_metadata 链路完整。"""
        import json
        from types import ModuleType, SimpleNamespace
        import sys

        # 注入假 google 模块（如果尚未注入）
        if "google" not in sys.modules or not hasattr(sys.modules.get("google", SimpleNamespace()), "genai"):
            google = ModuleType("google")
            google.api = ModuleType("google.api")
            google.api_core = ModuleType("google.api_core")
            google.api_core.exceptions = ModuleType("google.api_core.exceptions")
            google.genai = ModuleType("google.genai")
            google.genai.types = ModuleType("google.genai.types")
            google.genai.types.Tool = MagicMock(return_value=MagicMock())
            google.genai.types.GenerateContentConfig = MagicMock(return_value=MagicMock())
            sys.modules["google"] = google
            sys.modules["google.api"] = google.api
            sys.modules["google.api_core"] = google.api_core
            sys.modules["google.api_core.exceptions"] = google.api_core.exceptions
            sys.modules["google.genai"] = google.genai
            sys.modules["google.genai.types"] = google.genai.types

        from core.gemini_codec import gemini_response_to_unified

        # 构造 Gemini 风格的假响应对象
        fn_call = SimpleNamespace(
            name="search",
            args={"q": "test"},
            id="gemini_call_42",
        )
        part = SimpleNamespace(
            text=None,
            function_call=fn_call,
            thought_signature="sig_xyz789",
        )
        content = SimpleNamespace(parts=[part])
        candidate = SimpleNamespace(
            content=content,
            finish_reason=SimpleNamespace(name="STOP"),
        )
        response = SimpleNamespace(
            candidates=[candidate],
            usage_metadata=SimpleNamespace(total_token_count=15),
        )

        # 通过真实 codec 转换
        unified = gemini_response_to_unified(response)

        # 验证 unified 结构
        assert unified["tool_calls"][0]["id"] == "gemini_call_42"
        assert unified["tool_calls"][0]["provider_metadata"]["thought_signature"] == "sig_xyz789"

        # 喂给 Runtime
        invoker = MagicMock()
        call_count = [0]
        captured_messages_2nd = []

        def fake_sync(messages, tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return unified
            else:
                captured_messages_2nd.extend(messages)
                return {"content": "done", "tool_calls": [], "finish_reason": "stop",
                        "usage_total": 5, "empty": False}

        invoker.invoke_sync.side_effect = fake_sync
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="search results", tool_name="search",
        )

        runtime = AgentRuntime(invoker, se, max_steps=5)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "search test"}],
            tools=[{"type": "function", "function": {"name": "search"}}],
            ctx=_make_ctx(),
            stream=False,
        ))

        assert invoker.invoke_sync.call_count == 2

        # 断言第二轮收到的 assistant 消息保留 provider_metadata
        assistant_msgs = [m for m in captured_messages_2nd if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        tc = assistant_msgs[0]["tool_calls"][0]

        assert tc["id"] == "gemini_call_42"
        assert tc["function"]["name"] == "search"
        assert json.loads(tc["function"]["arguments"])["q"] == "test"
        assert tc["provider_metadata"]["thought_signature"] == "sig_xyz789"


# ================================================================
#  P1: max_output_tokens 真实生效
# ================================================================

class TestMaxTokensPassed:
    """effective_max_tokens 传入 invoker 调用。"""

    def test_stream_max_tokens_passed_to_invoker(self):
        """流式模式：effective_max_tokens 传入 invoke_stream。"""
        invoker = MagicMock()
        invoker.invoke_stream.return_value = iter(_make_stream_events(text="ok"))
        se = MagicMock()

        runtime = AgentRuntime(invoker, se, max_steps=3, max_tokens=2048)
        # ctx.max_output_tokens=512 < runtime.max_tokens=2048 -> effective=512
        events = list(runtime.run(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            ctx=_make_ctx(max_output_tokens=512),
        ))

        call_kwargs = invoker.invoke_stream.call_args
        assert call_kwargs.kwargs.get("max_tokens") == 512

    def test_sync_max_tokens_passed_to_invoker(self):
        """同步模式：effective_max_tokens 传入 invoke_sync。"""
        invoker = MagicMock()
        invoker.invoke_sync.return_value = _make_sync_result(content="ok")
        se = MagicMock()

        runtime = AgentRuntime(invoker, se, max_steps=3, max_tokens=2048)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            ctx=_make_ctx(max_output_tokens=256),
            stream=False,
        ))

        call_kwargs = invoker.invoke_sync.call_args
        assert call_kwargs.kwargs.get("max_tokens") == 256

    def test_runtime_max_tokens_used_when_ctx_larger(self):
        """ctx.max_output_tokens > runtime.max_tokens -> 以 runtime 为准。"""
        invoker = MagicMock()
        invoker.invoke_stream.return_value = iter(_make_stream_events(text="ok"))
        se = MagicMock()

        runtime = AgentRuntime(invoker, se, max_steps=3, max_tokens=1024)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            ctx=_make_ctx(max_output_tokens=4096),
        ))

        call_kwargs = invoker.invoke_stream.call_args
        assert call_kwargs.kwargs.get("max_tokens") == 1024


# ================================================================
#  P1: token_budget 后置熔断
# ================================================================

class TestTokenBudgetPostCheck:
    """步骤结束后也检查 token_budget，防止最后一轮超限成功。"""

    def test_budget_exceeded_after_step_with_tools(self):
        """步骤消耗后超限，且有工具调用 -> 不执行工具，直接终止。"""
        invoker = MagicMock()
        call_count = [0]

        def fake_stream(messages, tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一步消耗 100 tokens，返回工具调用
                yield from _make_stream_events(
                    tool_calls=[{"id": "c1", "name": "echo", "arguments": '{}'}],
                    usage={"prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100},
                )
            else:
                # 如果预算检查失败，不应该到达这里
                yield from _make_stream_events(text="should not reach")

        invoker.invoke_stream = fake_stream
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="ok", tool_name="echo",
        )

        runtime = AgentRuntime(invoker, se, max_steps=5)
        # budget=80，第一步消耗 100 -> 后置检查触发
        events = list(runtime.run(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            ctx=_make_ctx(token_budget=80),
        ))

        types = [e.type for e in events]
        assert RuntimeEventType.TOKEN_BUDGET_EXCEEDED in types
        assert RuntimeEventType.DONE in types

        # 预算超限后不执行有副作用的工具
        assert RuntimeEventType.TOOL_RESULT not in types
        se.execute_with_context.assert_not_called()

        # 确保第二步没有被执行（预算已耗尽）
        step_starts = [e for e in events if e.type == RuntimeEventType.STEP_START]
        assert len(step_starts) == 1

    def test_budget_exceeded_after_step_without_tools(self):
        """步骤消耗后超限，无工具调用 -> 直接终止。"""
        invoker = MagicMock()
        invoker.invoke_stream.return_value = iter(_make_stream_events(
            text="response",
            usage={"prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100},
        ))
        se = MagicMock()

        runtime = AgentRuntime(invoker, se, max_steps=5)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            ctx=_make_ctx(token_budget=80),
        ))

        types = [e.type for e in events]
        assert RuntimeEventType.TOKEN_BUDGET_EXCEEDED in types
        assert RuntimeEventType.DONE in types

        budget_event = [e for e in events if e.type == RuntimeEventType.TOKEN_BUDGET_EXCEEDED][0]
        assert budget_event.data["used"] == 100
        assert budget_event.data["budget"] == 80


# ================================================================
#  TOOL_RESULT 完整 output + display
# ================================================================

class TestToolResultOutputDisplay:
    """TOOL_RESULT 事件同时暴露完整 output 和截断 display。"""

    def test_short_output_equals_display(self):
        """短结果：output == display。"""
        invoker = MagicMock()
        invoker.invoke_stream.return_value = iter(_make_stream_events(tool_calls=[
            {"id": "c1", "name": "echo", "arguments": '{}'},
        ]))
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output="short result", tool_name="echo",
        )

        runtime = AgentRuntime(invoker, se, max_steps=3)
        # 第二轮返回文本
        invoker.invoke_stream.side_effect = None
        invoker.invoke_stream.return_value = iter(_make_stream_events(tool_calls=[
            {"id": "c1", "name": "echo", "arguments": '{}'},
        ]))

        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            ctx=_make_ctx(),
        ))

        tool_results = [e for e in events if e.type == RuntimeEventType.TOOL_RESULT]
        assert len(tool_results) == 1
        assert tool_results[0].data["output"] == "short result"
        assert tool_results[0].data["display"] == "short result"

    def test_long_output_truncated_in_display(self):
        """长结果：output 完整，display 截断。"""
        long_output = "x" * 2000
        invoker = MagicMock()
        call_count = [0]

        def fake_stream(messages, tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                yield from _make_stream_events(tool_calls=[
                    {"id": "c1", "name": "echo", "arguments": '{}'},
                ])
            else:
                yield from _make_stream_events(text="done")

        invoker.invoke_stream = fake_stream
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output=long_output, tool_name="echo",
        )

        runtime = AgentRuntime(invoker, se, max_steps=3)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            ctx=_make_ctx(),
        ))

        tool_results = [e for e in events if e.type == RuntimeEventType.TOOL_RESULT]
        assert len(tool_results) == 1
        # output 完整
        assert len(tool_results[0].data["output"]) == 2000
        # display 截断
        assert len(tool_results[0].data["display"]) < 2000
        assert "截断" in tool_results[0].data["display"]

    def test_full_output_persisted_in_messages(self):
        """完整 output 回填到 messages（供模型下一轮使用）。"""
        long_output = "data_" * 500  # 2500 chars
        invoker = MagicMock()
        call_count = [0]

        def fake_stream(messages, tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                yield from _make_stream_events(tool_calls=[
                    {"id": "c1", "name": "fetch", "arguments": '{}'},
                ])
            else:
                # 第二轮 messages 应包含完整 output
                tool_msgs = [m for m in messages if m.get("role") == "tool"]
                assert len(tool_msgs) == 1
                assert len(tool_msgs[0]["content"]) == 2500
                yield from _make_stream_events(text="done")

        invoker.invoke_stream = fake_stream
        se = MagicMock()
        se.execute_with_context.return_value = ExecutionResult(
            ok=True, output=long_output, tool_name="fetch",
        )

        runtime = AgentRuntime(invoker, se, max_steps=3)
        events = list(runtime.run(
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            ctx=_make_ctx(),
        ))

        assert RuntimeEventType.DONE in [e.type for e in events]

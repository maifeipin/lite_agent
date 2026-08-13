"""
Phase 0 — 行为锁定测试

锁定当前 skill_engine + worker_agent + agent 的关键行为，作为重构回归基线。
重构后所有测试必须继续通过，否则说明行为漂移。

测试分层：
  - 纯函数 / 工具引擎层：T01–T06, T08, T12, T14  → 直接驱动生产代码
  - WorkerAgent 层：T09, T10, T11/T15, T13         → 实例化 / mock 驱动生产路径
  - 旧签名兼容：T_compat                            → 锁定旧 execute() 签名
"""

import json
import os
import tempfile
import pytest
from unittest.mock import MagicMock, Mock, patch

import openai

from core.skill_engine import (
    _skill_registry,
    skill,
    SkillEngine,
    _cap_tool_result,
    MAX_TOOL_RESULT_LEN,
    _write_audit,
)
from core.execution import ExecutionContext, ExecutionResult, ExecutionSource, ActorType, SkillPolicy
from core.model_event import ModelEvent, ModelEventType
from core.utils.masker import mask_secrets
from core.worker_agent import WorkerAgent
from core.subtask_dag import Subtask, SubtaskType
from agent import Agent, IncomingMessage


# ================================================================
#  T01: 合法工具调用 → 驱动 SkillEngine.execute() 生产路径
# ================================================================
def test_t01_legal_tool_call(engine_with_test_skills):
    result = engine_with_test_skills.execute("test_echo", json.dumps({"text": "hello"}))
    assert "echo: hello" in result


# ================================================================
#  T02: 不存在的工具 → 驱动 SkillEngine.execute() 生产路径
# ================================================================
def test_t02_nonexistent_tool(engine):
    result = engine.execute("nonexistent_tool", "{}")
    assert "未知技能" in result


# ================================================================
#  T03: 非法 JSON 参数 → 驱动 SkillEngine.execute() 生产路径
# ================================================================
def test_t03_invalid_json_args(engine_with_test_skills):
    result = engine_with_test_skills.execute("test_echo", "not valid json")
    assert "参数解析失败" in result


# ================================================================
#  T04: 技能抛出异常 → 驱动 SkillEngine.execute() 生产路径
# ================================================================
def test_t04_skill_raises_exception(engine_with_test_skills):
    result = engine_with_test_skills.execute("test_raise", json.dumps({"msg": "test error"}))
    assert "技能执行异常" in result
    assert "test error" in result


# ================================================================
#  T05: 超长结果截断 → 驱动 _cap_tool_result 生产路径
# ================================================================
def test_t05_cap_tool_result_short():
    short = "hello world"
    assert _cap_tool_result("test", short) == short


def test_t05_cap_tool_result_long():
    long_result = "A" * (MAX_TOOL_RESULT_LEN + 1000)
    capped = _cap_tool_result("test", long_result)
    assert len(capped) <= MAX_TOOL_RESULT_LEN
    assert "已截断" in capped


def test_t05_cap_tool_result_exact_at_limit():
    exact = "B" * MAX_TOOL_RESULT_LEN
    assert _cap_tool_result("test", exact) == exact


# ================================================================
#  T06: 密钥脱敏 — 纯函数 + 集成测试（审计日志经过脱敏）
# ================================================================
def test_t06_mask_connection_string_password():
    masked = mask_secrets("postgresql://user:hunter2@localhost:5432/db")
    assert "hunter2" not in masked
    assert "***" in masked


def test_t06_mask_api_key():
    masked = mask_secrets('api_key="sk-1234567890abcdef"')
    assert "sk-1234567890abcdef" not in masked
    assert "***" in masked


def test_t06_mask_password_key():
    masked = mask_secrets("password=mysecret123")
    assert "mysecret123" not in masked
    assert "***" in masked


def test_t06_no_mask_plain_text():
    plain = "普通文本 without any secrets"
    assert mask_secrets(plain) == plain


def test_t06_audit_log_masked(engine_with_test_skills, tmp_path):
    """集成测试：SkillEngine.execute() 写入的审计日志必须经过脱敏。"""
    audit_file = tmp_path / "audit.log"

    # 注入临时审计日志路径
    with patch("core.skill_engine.AUDIT_LOG", str(audit_file)):
        engine_with_test_skills.execute(
            "test_echo",
            json.dumps({"text": "password=secret123 api_key=sk-abc"}),
        )

    assert audit_file.exists()
    content = audit_file.read_text()
    # 审计日志中不应出现明文密钥
    assert "secret123" not in content
    assert "sk-abc" not in content
    # 审计日志中应包含脱敏后的 ***
    assert "***" in content
    # 技能名出现在审计日志中
    assert "test_echo" in content


# ================================================================
#  T07: allowlist 语义 — LEGACY CHARACTERIZATION
#  当前 get_schemas_by_names([]) 返回全部 schema。
#  Phase 1c 后将改为返回空列表，届时以下断言需从 3 改为 0。
# ================================================================
def test_t07_LEGACY_allowlist_none_returns_all(engine_with_test_skills):
    """LEGACY: names=None → 返回全部。Phase 1c 后此行为不变。"""
    all_schemas = engine_with_test_skills.get_schemas_by_names(None)
    assert len(all_schemas) == 4


def test_t07_LEGACY_allowlist_empty_returns_all(engine_with_test_skills):
    """Phase 1c: names=[] → 返回空列表（禁止全部）。"""
    empty_schemas = engine_with_test_skills.get_schemas_by_names([])
    assert len(empty_schemas) == 0


def test_t07_allowlist_nonempty_filters(engine_with_test_skills):
    """非空 names 应正确过滤。Phase 1c 后此行为不变。"""
    filtered = engine_with_test_skills.get_schemas_by_names(["test_echo"])
    assert len(filtered) == 1
    assert filtered[0]["function"]["name"] == "test_echo"


# ================================================================
#  T08: guest 工具限制 → 驱动 SkillEngine 生产路径
# ================================================================
def test_t08_guest_ok_marking(engine_with_test_skills):
    assert engine_with_test_skills.is_guest_ok("test_guest_ok") is True
    assert engine_with_test_skills.is_guest_ok("test_echo") is False


def test_t08_guest_schemas_filtering(engine_with_test_skills):
    guest_schemas = engine_with_test_skills.get_guest_schemas()
    guest_names = [s["function"]["name"] for s in guest_schemas]
    assert "test_guest_ok" in guest_names
    assert "test_echo" not in guest_names
    assert "test_raise" not in guest_names


# ================================================================
#  T09: 死循环检测 → 驱动 LoopDetector 生产路径
# ================================================================
def test_t09_dead_loop_detection_streak(engine_with_test_skills):
    """连续 3 次相同指纹 → 触发死循环检测。"""
    from core.loop_detector import LoopDetector
    detector = LoopDetector()

    assert detector.check("test_echo", '{"text": "hello"}') is False
    # 第 2 次：不触发
    assert detector.check("test_echo", '{"text": "hello"}') is False
    # 第 3 次：触发
    assert detector.check("test_echo", '{"text": "hello"}') is True


def test_t09_dead_loop_resets_on_different_fingerprint(engine_with_test_skills):
    """不同指纹应重置 streak 计数。"""
    from core.loop_detector import LoopDetector
    detector = LoopDetector()

    detector.check("test_echo", '{"text": "a"}')
    detector.check("test_echo", '{"text": "a"}')
    # streak=2，现在换不同参数
    is_dead = detector.check("test_echo", '{"text": "b"}')
    assert is_dead is False  # 不同指纹，重置


# ================================================================
#  WorkerAgent._get_tools() 三态语义
# ================================================================
def test_worker_get_tools_none_allowlist_returns_all(engine_with_test_skills):
    """tools_allowlist=None -> 返回全部工具。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="test-model",
        model_cfg={"max_steps": 8, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
        tools_allowlist=None,
    )
    tools = worker._get_tools()
    assert len(tools) == engine_with_test_skills.get_skill_count()


def test_worker_get_tools_empty_allowlist_returns_empty(engine_with_test_skills):
    """tools_allowlist=[] -> 返回空列表（禁止全部，不返回全部工具）。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="test-model",
        model_cfg={"max_steps": 8, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
        tools_allowlist=[],
    )
    tools = worker._get_tools()
    assert tools == []


def test_worker_get_tools_partial_allowlist(engine_with_test_skills):
    """tools_allowlist=[...] -> 只返回指定工具。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="test-model",
        model_cfg={"max_steps": 8, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
        tools_allowlist=["test_echo"],
    )
    tools = worker._get_tools()
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "test_echo"


def test_worker_empty_allowlist_gemini_no_tools_in_request(engine_with_test_skills):
    """Worker 空 allowlist + Gemini driver → 模型请求中完全没有工具。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="gemini-model",
        model_cfg={"max_steps": 1, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
        tools_allowlist=[],
        driver="gemini_native",
    )

    subtask = Subtask(
        id="st1",
        name="test_subtask",
        type=SubtaskType.TEXT,
        prompt="do something without tools",
    )

    # mock invoke_sync 返回无 tool_calls 的结果，捕获 tools 参数
    captured_tools = []

    def fake_invoke_sync(messages, tools=None, **kwargs):
        captured_tools.append(tools)
        return {"content": "done", "tool_calls": [], "finish_reason": "stop",
                "usage_total": 0, "empty": False}

    worker.model_invoker.invoke_sync = fake_invoke_sync

    worker.run(subtask)

    # 断言模型请求中完全没有工具（空 allowlist → tools=[]）
    assert captured_tools == [[]]


# ================================================================
#  list_skills_filtered 空列表 -> 不展示任何工具
# ================================================================
def test_list_skills_filtered_empty_returns_none(engine_with_test_skills):
    """list_skills_filtered([]) -> 返回"(无可用工具)"，不展示全部。"""
    result = engine_with_test_skills.list_skills_filtered([])
    assert "无可用工具" in result


def test_list_skills_filtered_none_returns_all(engine_with_test_skills):
    """list_skills_filtered(None) -> 返回全部技能。"""
    result = engine_with_test_skills.list_skills_filtered(None)
    assert "test_echo" in result


def test_list_skills_filtered_partial(engine_with_test_skills):
    """list_skills_filtered(["test_echo"]) -> 只返回 test_echo。"""
    result = engine_with_test_skills.list_skills_filtered(["test_echo"])
    assert "test_echo" in result
    assert "test_search" not in result


# ================================================================
#  T10: 达到最大步数 -> mock invoke_sync 驱动 WorkerAgent.run() 循环
# ================================================================
def test_t10_max_steps_terminates(engine_with_test_skills):
    """mock _call_model 始终返回 tool_calls，验证 max_steps=2 时循环终止。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="test-model",
        model_cfg={"max_steps": 2, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
    )

    subtask = Subtask(
        id="st1",
        name="test_subtask",
        type=SubtaskType.TEXT,
        prompt="do something",
    )

    # mock _call_model 始终返回一个 tool_call
    def mock_call_model(messages, tools, **kwargs):
        return {
            "content": "",
            "tool_calls": [
                {"id": "call_1", "name": "test_echo", "arguments": '{"text": "x"}'}
            ],
            "finish_reason": "tool_calls",
            "usage_total": 100,
        }

    with patch.object(worker.model_invoker, "invoke_sync", side_effect=mock_call_model):
        reply, extracted = worker.run(subtask)

    # max_steps=2，循环 2 次后终止
    assert "步骤过多" in reply or "已自动终止" in reply
    # steps_used 应等于 max_steps
    assert subtask.steps_used == 2


# ================================================================
#  T11/T15: 多工具调用消息顺序 + tool_call_id 对应关系
#  mock _call_model 两轮调用，捕获第二轮 messages 断言顺序和 tool_call_id
# ================================================================
def test_t11_t15_multi_tool_call_message_order(engine_with_test_skills):
    """mock 多工具调用，捕获第二轮 messages 验证工具消息顺序和 tool_call_id。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="test-model",
        model_cfg={"max_steps": 3, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
    )

    subtask = Subtask(
        id="st1",
        name="test_subtask",
        type=SubtaskType.TEXT,
        prompt="do something",
    )

    call_count = [0]
    captured_round2_messages = []

    def mock_call_model(messages, tools, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return {
                "content": "let me call tools",
                "tool_calls": [
                    {"id": "call_aaa", "name": "test_echo", "arguments": '{"text": "a"}'},
                    {"id": "call_bbb", "name": "test_echo", "arguments": '{"text": "b"}'},
                ],
                "finish_reason": "tool_calls",
                "usage_total": 100,
            }
        else:
            # 第二轮调用时捕获 messages 列表
            captured_round2_messages.extend(messages)
            return {
                "content": "all done",
                "tool_calls": [],
                "finish_reason": "stop",
                "usage_total": 50,
            }

    with patch.object(worker.model_invoker, "invoke_sync", side_effect=mock_call_model):
        reply, extracted = worker.run(subtask)

    # 验证 run() 返回
    assert "all done" in reply
    assert len(extracted) == 2
    assert extracted[0]["name"] == "test_echo"
    assert extracted[1]["name"] == "test_echo"
    # P1 回归：extracted_tools 必须保留工具调用参数
    assert extracted[0]["args"] == '{"text": "a"}'
    assert extracted[1]["args"] == '{"text": "b"}'

    # === T11: 验证第二轮 messages 中的消息顺序 ===
    # 期望: assistant(tool_calls=[call_aaa, call_bbb]) → tool(call_aaa) → tool(call_bbb)
    roles = [m["role"] for m in captured_round2_messages]
    assistant_indices = [i for i, r in enumerate(roles) if r == "assistant"]
    tool_indices = [i for i, r in enumerate(roles) if r == "tool"]

    assert len(assistant_indices) >= 1, "应包含 assistant(tool_calls) 消息"
    assert len(tool_indices) == 2, "应包含 2 条 tool 消息"

    # assistant 消息必须在 tool 消息之前
    last_ai_idx = assistant_indices[-1]
    assert last_ai_idx < tool_indices[0], "assistant(tool_calls) 必须在 tool 消息之前"
    assert tool_indices[0] < tool_indices[1], "tool 消息按工具调用顺序排列"

    # assistant 消息中的 tool_calls
    last_assistant = captured_round2_messages[last_ai_idx]
    assert "tool_calls" in last_assistant
    assert len(last_assistant["tool_calls"]) == 2
    assert last_assistant["tool_calls"][0]["id"] == "call_aaa"
    assert last_assistant["tool_calls"][1]["id"] == "call_bbb"

    # === T15: 验证 tool_call_id 与 assistant.tool_calls[].id 对应 ===
    tool_msgs = [captured_round2_messages[i] for i in tool_indices]
    assert tool_msgs[0]["tool_call_id"] == "call_aaa"
    assert tool_msgs[1]["tool_call_id"] == "call_bbb"


# ================================================================
#  T12: Gemini function call 声明转换 → 驱动 SkillEngine 生产路径
# ================================================================
def test_t12_gemini_tool_declarations(engine_with_test_skills):
    decls = engine_with_test_skills.get_gemini_tool_declarations(["test_echo"])
    assert len(decls) == 1
    decl = decls[0]
    assert decl["name"] == "test_echo"
    assert "parameters" in decl
    assert decl["parameters"]["type"] == "OBJECT"
    assert "text" in decl["parameters"]["properties"]
    assert decl["parameters"]["properties"]["text"]["type"] == "STRING"


def test_t12_gemini_tool_declarations_all(engine_with_test_skills):
    decls = engine_with_test_skills.get_gemini_tool_declarations()
    assert len(decls) == 4


# ================================================================
#  T13: 不可重试异常分类 → 驱动 Agent._NON_RETRYABLE_EXCEPTIONS 生产路径
# ================================================================
def test_t13_non_retryable_exceptions():
    """验证 agent.py 中定义的不可重试异常类型。"""
    import openai
    from agent import _NON_RETRYABLE_EXCEPTIONS

    assert openai.BadRequestError in _NON_RETRYABLE_EXCEPTIONS
    assert openai.AuthenticationError in _NON_RETRYABLE_EXCEPTIONS
    assert openai.RateLimitError in _NON_RETRYABLE_EXCEPTIONS
    assert openai.NotFoundError in _NON_RETRYABLE_EXCEPTIONS
    assert openai.PermissionDeniedError in _NON_RETRYABLE_EXCEPTIONS


def test_t13_retry_condition_timeout_is_retryable():
    """APITimeoutError 不在不可重试列表中，应可重试。"""
    import openai
    from agent import _NON_RETRYABLE_EXCEPTIONS

    is_non_retryable = isinstance(
        openai.APITimeoutError("timeout"), _NON_RETRYABLE_EXCEPTIONS
    )
    assert is_non_retryable is False


def test_t13_retry_condition_bad_request_is_not_retryable():
    """BadRequestError 不可重试。"""
    import openai
    from agent import _NON_RETRYABLE_EXCEPTIONS

    is_non_retryable = isinstance(
        openai.BadRequestError("bad", response=MagicMock(), body=None),
        _NON_RETRYABLE_EXCEPTIONS,
    )
    assert is_non_retryable is True


# ================================================================
#  T14: Token 估算 → 驱动 _estimate_tokens 生产路径
# ================================================================
def test_t14_estimate_tokens():
    from agent import _estimate_tokens

    messages = [{"role": "user", "content": "Hello, how are you?"}]
    completion = "I'm fine, thank you!"
    usage = _estimate_tokens(messages, completion)
    assert "prompt_tokens" in usage
    assert "completion_tokens" in usage
    assert "total_tokens" in usage
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_t14_estimate_tokens_empty():
    from agent import _estimate_tokens

    usage = _estimate_tokens([], "")
    assert usage["total_tokens"] >= 1


# ================================================================
#  T13: 流式 AI Loop 重试/不重试 → 驱动 _stream_ai_loop() 生产路径
#  用假流式客户端 + 最小 Agent 实例，验证 timeout 重试和 4xx 不重试
# ================================================================


def _make_minimal_agent():
    """构造一个所有外部依赖均已 mock 的 Agent 实例，仅用于测试 _stream_ai_loop。"""
    config = {
        "llm": {
            "api_key": "test-key",
            "base_url": "http://localhost",
            "model": "test-model",
            "max_tokens": 256,
            "temperature": 0.3,
        },
        "session": {"max_steps_per_goal": 3},
        "bot_name": "TestBot",
        "service_name": "test-svc",
    }

    with patch.object(SkillEngine, '_load_skills', return_value=None):
        agent = Agent(config)

    # Mock session manager
    mock_session = MagicMock()
    mock_session.token_usage = 0
    mock_session.status = "idle"
    agent.session_mgr.get_or_create = MagicMock(return_value=mock_session)
    agent.session_mgr.add_message = MagicMock()
    agent.session_mgr.get_history = MagicMock(return_value=[])
    agent.session_mgr.log_api_usage = MagicMock()
    agent.session_mgr.mark_done = MagicMock()

    # Mock skill engine
    agent.skill_engine.get_all_schemas = MagicMock(return_value=[])
    agent.skill_engine.get_guest_schemas = MagicMock(return_value=[])
    agent.skill_engine.get_guard_prompts = MagicMock(return_value=[])
    agent.skill_engine.list_skills = MagicMock(return_value="no skills")

    # Mock client
    agent.client = MagicMock()

    # Mock dead loop counter
    agent._dead_loop_counter = {}

    # Disable memory
    agent.memory = None

    return agent


def test_t13_stream_retry_on_timeout():
    """APITimeoutError 首字未生成 → 自动重试一次 → 成功。"""
    agent = _make_minimal_agent()
    msg = IncomingMessage(
        channel="test", user_id="u1", chat_id="c1",
        message_id="m1", text="hello",
    )

    call_count = [0]

    def fake_invoke_stream(messages, tools=None, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise openai.APITimeoutError("timeout")
        else:
            yield ModelEvent(type=ModelEventType.TEXT, data="hello world")
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "stop"})

    agent.model_invoker.invoke_stream = fake_invoke_stream

    events = list(agent._stream_ai_loop(msg))

    # 验证重试了一次（共 2 次调用）
    assert call_count[0] == 2

    # 验证事件类型：应有 token 和 done，没有 error
    event_types = [e["type"] for e in events]
    assert "token" in event_types
    assert "done" in event_types
    assert "error" not in event_types


def test_t13_stream_no_retry_on_4xx():
    """BadRequestError 不可重试 → 立即 yield error。"""
    agent = _make_minimal_agent()
    msg = IncomingMessage(
        channel="test", user_id="u1", chat_id="c1",
        message_id="m1", text="hello",
    )

    def fake_invoke_stream(messages, tools=None, **kwargs):
        raise openai.BadRequestError("bad request", response=MagicMock(), body=None)

    agent.model_invoker.invoke_stream = fake_invoke_stream

    events = list(agent._stream_ai_loop(msg))

    event_types = [e["type"] for e in events]
    assert "error" in event_types
    assert "done" in event_types
    # 不应生成 token
    assert "token" not in event_types


# ================================================================
#  补充：旧签名兼容性
# ================================================================
def test_compat_execute_signature(engine_with_test_skills):
    """旧 execute(name, arguments) 签名必须保持可用。"""
    result = engine_with_test_skills.execute("test_echo", '{"text": "backward"}')
    assert "backward" in result


def test_compat_skill_engine_registry_count(engine_with_test_skills):
    assert engine_with_test_skills.get_skill_count() == 4


def test_compat_skill_engine_get_all_names(engine_with_test_skills):
    names = engine_with_test_skills.get_all_names()
    assert names == {"test_echo", "test_raise", "test_guest_ok", "test_write"}


# ================================================================
#  WorkerAgent 补充：_call_model 返回结构
# ================================================================
def test_worker_call_model_return_structure(engine_with_test_skills):
    """验证 _call_model 返回的 dict 结构（mock 后）。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="test-model",
        model_cfg={"max_steps": 8, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
    )

    # mock client.chat.completions.create
    from unittest.mock import Mock
    mock_tc = Mock()
    mock_tc.id = "call_x"
    mock_tc.function.name = "test_echo"
    mock_tc.function.arguments = '{"text":"x"}'

    mock_choice = Mock()
    mock_choice.finish_reason = "tool_calls"
    mock_choice.message.content = ""
    mock_choice.message.tool_calls = [mock_tc]

    mock_usage = Mock()
    mock_usage.total_tokens = 42

    mock_response = Mock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    worker.client.chat.completions.create.return_value = mock_response

    result = worker.model_invoker.invoke_sync([{"role": "user", "content": "hi"}], [])

    assert result["content"] == ""
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["id"] == "call_x"
    assert result["tool_calls"][0]["name"] == "test_echo"
    assert result["usage_total"] == 42
    assert result["finish_reason"] == "tool_calls"


# ================================================================
#  WorkerAgent 补充：空 tool_calls 时返回最终回复
# ================================================================
def test_worker_run_returns_final_reply(engine_with_test_skills):
    """mock _call_model 返回无 tool_calls，验证 run() 返回最终回复。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="test-model",
        model_cfg={"max_steps": 3, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
    )

    subtask = Subtask(
        id="st1",
        name="test_subtask",
        type=SubtaskType.TEXT,
        prompt="say hello",
    )

    def mock_call_model(messages, tools, **kwargs):
        return {
            "content": "Hello, world!",
            "tool_calls": [],
            "finish_reason": "stop",
            "usage_total": 50,
        }

    with patch.object(worker.model_invoker, "invoke_sync", side_effect=mock_call_model):
        reply, extracted = worker.run(subtask)

    assert "Hello, world!" in reply
    assert extracted == []  # 无工具调用


# ================================================================
#  Phase 2d — Worker 迁移到 AgentRuntime 的契约回归测试
#  - extracted_tools 保留工具参数 (P1)
#  - Worker 审计来源 = ORCHESTRATOR (P1)
#  - 空响应语义：empty=True → "(空回复 - 安全过滤)"，
#                empty=False 且 content="" → "(空回复)"
# ================================================================
def test_worker_audit_source_is_orchestrator(engine_with_test_skills):
    """Worker 构造的 ExecutionContext.source 必须是 ORCHESTRATOR，而非 DIRECT。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="test-model",
        model_cfg={"max_steps": 1, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
    )

    captured_ctxs = []

    def fake_execute_with_context(ctx, name, args):
        captured_ctxs.append(ctx)
        return ExecutionResult.success(name, {"raw": args}, "ok")

    subtask = Subtask(id="st1", name="t", type=SubtaskType.TEXT, prompt="x")

    def mock_call_model(messages, tools, **kwargs):
        return {"content": "done", "tool_calls": [], "finish_reason": "stop",
                "usage_total": 0, "empty": False}

    with patch.object(worker.model_invoker, "invoke_sync", side_effect=mock_call_model), \
         patch.object(engine_with_test_skills, "execute_with_context",
                      side_effect=fake_execute_with_context):
        worker.run(subtask)

    # 没有工具调用时不会捕获到 ctx，需要让模型返回一个 tool_call
    def mock_call_model_with_tool(messages, tools, **kwargs):
        return {
            "content": "",
            "tool_calls": [
                {"id": "c1", "name": "test_echo", "arguments": '{"text":"hi"}'}
            ],
            "finish_reason": "tool_calls",
            "usage_total": 10,
            "empty": False,
        }

    def mock_call_model_final(messages, tools, **kwargs):
        return {"content": "done", "tool_calls": [], "finish_reason": "stop",
                "usage_total": 0, "empty": False}

    call_count = [0]
    def mock_two_phase(messages, tools, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return mock_call_model_with_tool(messages, tools, **kwargs)
        return mock_call_model_final(messages, tools, **kwargs)

    captured_ctxs.clear()
    with patch.object(worker.model_invoker, "invoke_sync", side_effect=mock_two_phase), \
         patch.object(engine_with_test_skills, "execute_with_context",
                      side_effect=fake_execute_with_context):
        worker.run(subtask)

    assert len(captured_ctxs) == 1
    ctx = captured_ctxs[0]
    assert ctx.source == ExecutionSource.ORCHESTRATOR
    assert ctx.actor_type == ActorType.WORKER


def test_worker_empty_response_safety_filter(engine_with_test_skills):
    """Gemini 安全过滤/无候选 (empty=True) → Worker 返回 '(空回复 - 安全过滤)'。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="gemini-model",
        model_cfg={"max_steps": 1, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
        driver="gemini_native",
    )

    subtask = Subtask(id="st1", name="t", type=SubtaskType.TEXT, prompt="x")

    def mock_safety_filtered(messages, tools, **kwargs):
        return {"content": "", "tool_calls": [], "finish_reason": "SAFETY",
                "usage_total": 0, "empty": True}

    with patch.object(worker.model_invoker, "invoke_sync", side_effect=mock_safety_filtered):
        reply, extracted = worker.run(subtask)

    assert reply == "(空回复 - 安全过滤)"
    assert extracted == []


def test_worker_empty_response_normal_empty(engine_with_test_skills):
    """OpenAI 普通空响应 (empty=False, content='') → Worker 返回 '(空回复)'。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="test-model",
        model_cfg={"max_steps": 1, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
    )

    subtask = Subtask(id="st1", name="t", type=SubtaskType.TEXT, prompt="x")

    def mock_empty_reply(messages, tools, **kwargs):
        return {"content": "", "tool_calls": [], "finish_reason": "stop",
                "usage_total": 0, "empty": False}

    with patch.object(worker.model_invoker, "invoke_sync", side_effect=mock_empty_reply):
        reply, extracted = worker.run(subtask)

    assert reply == "(空回复)"
    assert extracted == []


def test_worker_extracted_tools_preserves_arguments(engine_with_test_skills):
    """P1 回归：extracted_tools 的 args 字段必须包含完整的工具调用参数。"""
    worker = WorkerAgent(
        name="test_worker",
        client=MagicMock(),
        model_name="test-model",
        model_cfg={"max_steps": 3, "max_tokens": 2048, "temperature": 0.3},
        skill_engine=engine_with_test_skills,
    )

    subtask = Subtask(id="st1", name="t", type=SubtaskType.TEXT, prompt="x")

    call_count = [0]

    def mock_call_model(messages, tools, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return {
                "content": "",
                "tool_calls": [
                    {"id": "c1", "name": "test_echo",
                     "arguments": '{"text": "hello world"}'},
                ],
                "finish_reason": "tool_calls",
                "usage_total": 50,
                "empty": False,
            }
        return {"content": "done", "tool_calls": [], "finish_reason": "stop",
                "usage_total": 10, "empty": False}

    with patch.object(worker.model_invoker, "invoke_sync", side_effect=mock_call_model):
        reply, extracted = worker.run(subtask)

    assert len(extracted) == 1
    assert extracted[0]["name"] == "test_echo"
    # args 必须是原始 arguments 字符串，不能丢失
    assert extracted[0]["args"] == '{"text": "hello world"}'


# ================================================================
#  Phase 1b — execute_with_context 新接口测试
# ================================================================

class TestExecuteWithContext:
    """覆盖 execute_with_context 的权限、guest、错误码和成功结果。"""

    # ---- 权限：allowed_tools ----

    def test_unrestricted_allows_any_skill(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1", allowed_tools=None)
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "hi"}))
        assert r.ok
        assert "echo: hi" in r.output

    def test_empty_frozenset_denies_all(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1", allowed_tools=frozenset())
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "hi"}))
        assert not r.ok
        assert r.error_code == "PERMISSION_DENIED"

    def test_nonempty_allowlist_allows_listed(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1", allowed_tools=["test_echo", "test_guest_ok"])
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "ok"}))
        assert r.ok
        assert "echo: ok" in r.output

    def test_nonempty_allowlist_denies_unlisted(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1", allowed_tools=["test_guest_ok"])
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "hi"}))
        assert not r.ok
        assert r.error_code == "PERMISSION_DENIED"

    def test_permission_denied_before_registry_check(self, engine_with_test_skills):
        """即使技能不存在，权限检查也先于 registry 检查，防止枚举。"""
        ctx = ExecutionContext(actor_id="u1", allowed_tools=["test_echo"])
        r = engine_with_test_skills.execute_with_context(
            ctx, "nonexistent_skill", "{}")
        assert not r.ok
        assert r.error_code == "PERMISSION_DENIED"

    # ---- Guest 检查 ----

    def test_guest_allowed_on_guest_ok_skill(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1", actor_type=ActorType.GUEST)
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_guest_ok", "{}")
        assert r.ok
        assert "guest allowed" in r.output

    def test_guest_forbidden_on_non_guest_skill(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1", actor_type=ActorType.GUEST)
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "hi"}))
        assert not r.ok
        assert r.error_code == "GUEST_FORBIDDEN"

    def test_non_guest_allowed_on_non_guest_skill(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1", actor_type=ActorType.USER)
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "hi"}))
        assert r.ok

    # ---- 错误码 ----

    def test_unknown_skill(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1")
        r = engine_with_test_skills.execute_with_context(
            ctx, "nonexistent_skill", "{}")
        assert not r.ok
        assert r.error_code == "UNKNOWN_SKILL"
        assert "未知技能" in r.output

    def test_invalid_args(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1")
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", "not valid json")
        assert not r.ok
        assert r.error_code == "INVALID_ARGS"
        assert "参数解析失败" in r.output

    def test_skill_error(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1")
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_raise", json.dumps({"msg": "boom"}))
        assert not r.ok
        assert r.error_code == "SKILL_ERROR"
        assert "技能执行异常" in r.output
        assert "boom" in r.output

    # ---- 成功结果结构 ----

    def test_success_result_structure(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1")
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "hello"}))
        assert r.ok is True
        assert r.tool_name == "test_echo"
        assert r.tool_args["text"] == "hello"
        assert "echo: hello" in r.output
        assert r.error_code == ""
        assert r.retryable is False

    def test_success_result_is_execution_result(self, engine_with_test_skills):
        ctx = ExecutionContext(actor_id="u1")
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "test"}))
        assert isinstance(r, ExecutionResult)

    # ---- 旧 execute() 委托验证 ----

    def test_old_execute_delegates_to_new(self, engine_with_test_skills):
        """旧 execute() 必须通过 execute_with_context 实现，不能独立实现。"""
        with patch.object(
            engine_with_test_skills, "execute_with_context",
            wraps=engine_with_test_skills.execute_with_context,
        ) as spy:
            result = engine_with_test_skills.execute(
                "test_echo", json.dumps({"text": "delegated"}))

        spy.assert_called_once()
        call_args = spy.call_args
        ctx = call_args[0][0]
        assert isinstance(ctx, ExecutionContext)
        assert ctx.source == ExecutionSource.LEGACY
        assert ctx.allowed_tools is None
        assert "delegated" in result

    def test_old_execute_result_matches_new_to_legacy_string(self, engine_with_test_skills):
        """旧 execute() 返回的字符串应等于 execute_with_context().to_legacy_string()。"""
        ctx = ExecutionContext(
            actor_id="legacy",
            source=ExecutionSource.LEGACY,
            allowed_tools=None,
        )
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "match"}))
        legacy = engine_with_test_skills.execute(
            "test_echo", json.dumps({"text": "match"}))
        assert legacy == r.to_legacy_string()


# ================================================================
#  Phase 1c — SkillPolicy 接入、dry-run、结构化审计
# ================================================================

class TestPhase1c:
    """覆盖 SkillPolicy 接入 registry、dry-run、结构化审计。"""

    # ---- Policy 存储在 registry ----

    def test_policy_stored_in_registry(self, engine_with_test_skills):
        """每个 registry entry 都包含 SkillPolicy 对象。"""
        from core.skill_engine import _skill_registry
        for name in ["test_echo", "test_raise", "test_guest_ok", "test_write"]:
            info = _skill_registry[name]
            assert "policy" in info
            assert isinstance(info["policy"], SkillPolicy)

    def test_policy_side_effect_values(self, engine_with_test_skills):
        """验证各技能的 side_effect 值。"""
        from core.skill_engine import _skill_registry
        assert _skill_registry["test_echo"]["policy"].side_effect is False
        assert _skill_registry["test_raise"]["policy"].side_effect is None
        assert _skill_registry["test_guest_ok"]["policy"].side_effect is False
        assert _skill_registry["test_write"]["policy"].side_effect is True

    def test_supports_dry_run_derived_from_handler(self, engine_with_test_skills):
        """supports_dry_run 从 dry_run_handler 是否存在派生，不可自由声明。"""
        from core.skill_engine import _skill_registry
        p_echo = _skill_registry["test_echo"]["policy"]
        p_raise = _skill_registry["test_raise"]["policy"]
        p_write = _skill_registry["test_write"]["policy"]
        assert p_echo.supports_dry_run is True   # 有 handler
        assert p_raise.supports_dry_run is False  # 无 handler
        assert p_write.supports_dry_run is True   # 有 handler

    # ---- dry-run 语义 ----

    def test_dry_run_handler_called_func_not_called(self, engine_with_test_skills):
        """dry-run 时调用 dry_run_handler，主函数不被调用。"""
        from core.skill_engine import _skill_registry
        from unittest.mock import Mock

        func_spy = Mock(wraps=_skill_registry["test_write"]["func"])
        _skill_registry["test_write"]["func"] = func_spy

        ctx = ExecutionContext(actor_id="u1", is_dry_run=True)
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_write", json.dumps({"data": "hello"}))

        assert r.ok
        assert "[dry-run] would write: hello" in r.output
        func_spy.assert_not_called()

    def test_dry_run_side_effect_false(self, engine_with_test_skills):
        """纯查询技能（side_effect=False）support dry-run。"""
        from core.skill_engine import _skill_registry
        from unittest.mock import Mock

        func_spy = Mock(wraps=_skill_registry["test_echo"]["func"])
        _skill_registry["test_echo"]["func"] = func_spy

        ctx = ExecutionContext(actor_id="u1", is_dry_run=True)
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "hi"}))

        assert r.ok
        assert "echo: hi" in r.output
        assert r.side_effects_performed == ()
        func_spy.assert_not_called()

    def test_dry_run_rejected_unknown_side_effect(self, engine_with_test_skills):
        """副作用未知（side_effect=None）拒绝 dry-run，主函数不被调用。"""
        from core.skill_engine import _skill_registry
        from unittest.mock import Mock

        func_spy = Mock(wraps=_skill_registry["test_raise"]["func"])
        _skill_registry["test_raise"]["func"] = func_spy

        ctx = ExecutionContext(actor_id="u1", is_dry_run=True)
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_raise", json.dumps({"msg": "x"}))

        assert not r.ok
        assert r.error_code == "DRY_RUN_REJECTED"
        func_spy.assert_not_called()

    def test_dry_run_ignored_when_not_dry_mode(self, engine_with_test_skills):
        """is_dry_run=False 时正常执行，不触发 dry-run 路径。"""
        ctx = ExecutionContext(actor_id="u1", is_dry_run=False)
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "real"}))
        assert r.ok
        assert "echo: real" in r.output
        assert "[dry-run]" not in r.output

    # ---- 结构化审计 — 统一出口 ----

    def _audit_for(self, engine_with_test_skills, ctx, skill_name, args,
                   tmp_path, outcome, error_code=""):
        """辅助：执行一次调用并返回审计日志内容。"""
        import core.skill_engine as se
        log_path = tmp_path / "audit.log"
        with patch.object(se, "AUDIT_LOG", str(log_path)):
            engine_with_test_skills.execute_with_context(ctx, skill_name, args)
        return log_path.read_text(encoding="utf-8")

    def test_audit_success(self, engine_with_test_skills, tmp_path):
        ctx = ExecutionContext(actor_id="user_1", source=ExecutionSource.DIRECT)
        content = self._audit_for(engine_with_test_skills, ctx, "test_echo",
                                  json.dumps({"text": "ok"}), tmp_path, "success")
        assert "outcome=success" in content
        assert "actor=user_1" in content
        assert "source=direct" in content
        assert "error_code" not in content

    def test_audit_dry_run(self, engine_with_test_skills, tmp_path):
        ctx = ExecutionContext(actor_id="user_1", source=ExecutionSource.DIRECT,
                               is_dry_run=True)
        content = self._audit_for(engine_with_test_skills, ctx, "test_echo",
                                  json.dumps({"text": "x"}), tmp_path, "dry_run")
        assert "outcome=dry_run" in content
        assert "dry_run=1" in content

    def test_audit_permission_denied(self, engine_with_test_skills, tmp_path):
        ctx = ExecutionContext(actor_id="u1", allowed_tools=frozenset())
        content = self._audit_for(engine_with_test_skills, ctx, "test_echo",
                                  json.dumps({"text": "x"}), tmp_path, "denied",
                                  "PERMISSION_DENIED")
        assert "outcome=denied" in content
        assert "error_code=PERMISSION_DENIED" in content

    def test_audit_unknown_skill(self, engine_with_test_skills, tmp_path):
        ctx = ExecutionContext(actor_id="u1")
        content = self._audit_for(engine_with_test_skills, ctx, "no_such_skill",
                                  "{}", tmp_path, "denied", "UNKNOWN_SKILL")
        assert "outcome=denied" in content
        assert "error_code=UNKNOWN_SKILL" in content

    def test_audit_guest_forbidden(self, engine_with_test_skills, tmp_path):
        ctx = ExecutionContext(actor_id="u1", actor_type=ActorType.GUEST)
        content = self._audit_for(engine_with_test_skills, ctx, "test_echo",
                                  json.dumps({"text": "x"}), tmp_path, "denied",
                                  "GUEST_FORBIDDEN")
        assert "outcome=denied" in content
        assert "error_code=GUEST_FORBIDDEN" in content

    def test_audit_invalid_args(self, engine_with_test_skills, tmp_path):
        ctx = ExecutionContext(actor_id="u1")
        content = self._audit_for(engine_with_test_skills, ctx, "test_echo",
                                  "not json", tmp_path, "denied", "INVALID_ARGS")
        assert "outcome=denied" in content
        assert "error_code=INVALID_ARGS" in content

    def test_audit_dry_run_rejected(self, engine_with_test_skills, tmp_path):
        ctx = ExecutionContext(actor_id="u1", is_dry_run=True)
        content = self._audit_for(engine_with_test_skills, ctx, "test_raise",
                                  json.dumps({"msg": "x"}), tmp_path, "denied",
                                  "DRY_RUN_REJECTED")
        assert "outcome=denied" in content
        assert "error_code=DRY_RUN_REJECTED" in content

    def test_audit_skill_error(self, engine_with_test_skills, tmp_path):
        ctx = ExecutionContext(actor_id="u1")
        content = self._audit_for(engine_with_test_skills, ctx, "test_raise",
                                  json.dumps({"msg": "boom"}), tmp_path, "error",
                                  "SKILL_ERROR")
        assert "outcome=error" in content
        assert "error_code=SKILL_ERROR" in content

    def test_audit_args_redacted(self, engine_with_test_skills, tmp_path):
        """审计日志中的参数应经过脱敏。"""
        import core.skill_engine as se
        log_path = tmp_path / "audit.log"
        with patch.object(se, "AUDIT_LOG", str(log_path)):
            ctx = ExecutionContext(actor_id="u1")
            engine_with_test_skills.execute_with_context(
                ctx, "test_echo", json.dumps({"text": "api_key: sk-abc123secret"}))
        content = log_path.read_text(encoding="utf-8")
        assert "sk-abc123secret" not in content
        assert "***" in content

    def test_legacy_audit_format(self, engine_with_test_skills, tmp_path):
        """Legacy 路径的审计日志记录 actor=legacy source=legacy。"""
        import core.skill_engine as se
        log_path = tmp_path / "audit.log"
        with patch.object(se, "AUDIT_LOG", str(log_path)):
            engine_with_test_skills.execute(
                "test_echo", json.dumps({"text": "legacy_test"}))

        content = log_path.read_text(encoding="utf-8")
        assert "test_echo" in content
        assert "outcome=success" in content
        assert "actor=legacy" in content
        assert "source=legacy" in content

    # ---- 边界测试 ----

    def test_dry_run_handler_exception(self, engine_with_test_skills, tmp_path):
        """dry_run_handler 抛异常不应逃逸，返回结构化错误并写 error 审计。"""
        from core.skill_engine import _skill_registry
        import core.skill_engine as se

        # 注入一个会抛异常的 handler
        def _boom(**kwargs):
            raise RuntimeError("handler boom")
        _skill_registry["test_echo"]["dry_run_handler"] = _boom

        log_path = tmp_path / "audit.log"
        with patch.object(se, "AUDIT_LOG", str(log_path)):
            ctx = ExecutionContext(actor_id="u1", is_dry_run=True)
            r = engine_with_test_skills.execute_with_context(
                ctx, "test_echo", json.dumps({"text": "x"}))

        assert not r.ok
        assert r.error_code == "DRY_RUN_ERROR"
        assert "handler boom" in r.output

        content = log_path.read_text(encoding="utf-8")
        assert "outcome=error" in content
        assert "error_code=DRY_RUN_ERROR" in content

    def test_dry_run_handler_result_truncated(self, engine_with_test_skills):
        """dry_run_handler 返回超长文本时结果被截断。"""
        from core.skill_engine import _skill_registry, MAX_TOOL_RESULT_LEN

        _skill_registry["test_echo"]["dry_run_handler"] = lambda text: "x" * (MAX_TOOL_RESULT_LEN + 500)

        ctx = ExecutionContext(actor_id="u1", is_dry_run=True)
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "x"}))

        assert r.ok
        assert len(r.output) <= MAX_TOOL_RESULT_LEN + 100  # 截断提示可能追加少量字符

    def test_can_dry_run_without_handler_rejected(self, engine_with_test_skills):
        """can_dry_run=True 但无 dry_run_handler 时拒绝，不假造成功。"""
        from core.skill_engine import _skill_registry

        # 手工构造：policy.can_dry_run=True 但 registry 中没有 handler
        _skill_registry["test_echo"]["policy"] = SkillPolicy(
            side_effect=False, supports_dry_run=True, guest_ok=False)
        _skill_registry["test_echo"]["dry_run_handler"] = None

        ctx = ExecutionContext(actor_id="u1", is_dry_run=True)
        r = engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "x"}))

        assert not r.ok
        assert r.error_code == "DRY_RUN_REJECTED"
        assert "未注册 dry_run_handler" in r.output

    # ---- 拒绝的技能函数调用次数为零 ----

    def test_func_not_called_on_permission_denied(self, engine_with_test_skills):
        from core.skill_engine import _skill_registry
        from unittest.mock import Mock
        func_spy = Mock(wraps=_skill_registry["test_echo"]["func"])
        _skill_registry["test_echo"]["func"] = func_spy

        ctx = ExecutionContext(actor_id="u1", allowed_tools=frozenset())
        engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "x"}))
        func_spy.assert_not_called()

    def test_func_not_called_on_guest_forbidden(self, engine_with_test_skills):
        from core.skill_engine import _skill_registry
        from unittest.mock import Mock
        func_spy = Mock(wraps=_skill_registry["test_echo"]["func"])
        _skill_registry["test_echo"]["func"] = func_spy

        ctx = ExecutionContext(actor_id="u1", actor_type=ActorType.GUEST)
        engine_with_test_skills.execute_with_context(
            ctx, "test_echo", json.dumps({"text": "x"}))
        func_spy.assert_not_called()

    def test_func_not_called_on_invalid_args(self, engine_with_test_skills):
        from core.skill_engine import _skill_registry
        from unittest.mock import Mock
        func_spy = Mock(wraps=_skill_registry["test_echo"]["func"])
        _skill_registry["test_echo"]["func"] = func_spy

        ctx = ExecutionContext(actor_id="u1")
        engine_with_test_skills.execute_with_context(
            ctx, "test_echo", "not json")
        func_spy.assert_not_called()

    # ---- guard_prompts 使用 policy ----

    def test_guard_prompts_uses_policy(self, engine_with_test_skills):
        """get_guard_prompts 应从 SkillPolicy 读取 guard_keywords。"""
        from core.skill_engine import _skill_registry
        _skill_registry["test_echo"]["policy"] = SkillPolicy(
            guest_ok=True,
            guard_keywords=["alert", "urgent"],
            guard_prompt="请勿捏造告警信息",
            guard_threshold=1,
        )
        prompts = engine_with_test_skills.get_guard_prompts("this is an alert message")
        assert len(prompts) == 1
        assert "请勿捏造告警信息" in prompts[0]

    def test_guard_prompts_below_threshold(self, engine_with_test_skills):
        """匹配数低于 threshold 不触发 guard。"""
        from core.skill_engine import _skill_registry
        _skill_registry["test_echo"]["policy"] = SkillPolicy(
            guest_ok=True,
            guard_keywords=["alert", "urgent"],
            guard_prompt="请勿捏造告警信息",
            guard_threshold=2,
        )
        prompts = engine_with_test_skills.get_guard_prompts("this is an alert")
        assert len(prompts) == 0  # 只匹配 1 个，threshold=2

    # ---- 空 allowlist 的 Schema 查询 ----

    def test_empty_allowlist_schema_returns_empty(self, engine_with_test_skills):
        """get_schemas_by_names([]) 返回空列表。"""
        assert engine_with_test_skills.get_schemas_by_names([]) == []

    def test_none_allowlist_schema_returns_all(self, engine_with_test_skills):
        """get_schemas_by_names(None) 返回全部。"""
        assert len(engine_with_test_skills.get_schemas_by_names(None)) == 4


# ================================================================
#  Phase 2e — 主 Agent 端到端回归测试
#  覆盖: 纯文本/工具循环/访客拒绝/流式重试/Token 记账/
#        Session 消息顺序/Memory 回调/各终止事件
# ================================================================


def _make_e2e_agent():
    """构造一个端到端 Agent 实例:
    - 注册 test_echo / test_guest_ok 真实技能
    - mock model_invoker.invoke_stream (通过 fake stream 函数注入)
    - mock SessionManager.add_message (记录调用顺序)
    - mock log_api_usage / mark_done / increment_tool_calls
    - mock memory.before_reply / after_reply
    """
    _skill_registry.clear()

    @skill(
        name="test_echo",
        description="回显输入",
        params={"text": {"type": "string", "description": "要回显的文本"}},
        side_effect=False,
        guest_ok=False,
    )
    def test_echo(text: str) -> str:
        return f"echo: {text}"

    @skill(
        name="test_guest_ok",
        description="访客可用",
        params={},
        guest_ok=True,
        side_effect=False,
    )
    def test_guest_ok() -> str:
        return "guest allowed"

    config = {
        "llm": {
            "api_key": "test-key",
            "base_url": "http://localhost",
            "model": "test-model",
            "max_tokens": 256,
            "temperature": 0.3,
        },
        "session": {"max_steps_per_goal": 3},
        "bot_name": "TestBot",
        "service_name": "test-svc",
    }

    with patch.object(SkillEngine, '_load_skills', return_value=None):
        agent = Agent(config)

    # Mock SessionManager
    mock_session = MagicMock()
    mock_session.token_usage = 0
    mock_session.status = "idle"
    agent.session_mgr.get_or_create = MagicMock(return_value=mock_session)
    # add_message 真实记录调用参数, 用于断言消息顺序
    agent.session_mgr.add_message = MagicMock()
    agent.session_mgr.get_history = MagicMock(return_value=[])
    agent.session_mgr.log_api_usage = MagicMock()
    agent.session_mgr.mark_done = MagicMock()
    agent.session_mgr.increment_tool_calls = MagicMock()

    # Mock memory
    agent.memory = MagicMock()
    agent.memory.before_reply = MagicMock(return_value="")

    return agent


def _stream_text(text: str, usage: dict = None):
    """生成一个返回纯文本的 fake invoke_stream。"""
    if usage is None:
        usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    def fake_invoke_stream(messages, tools=None, **kwargs):
        yield ModelEvent(type=ModelEventType.TEXT, data=text)
        yield ModelEvent(type=ModelEventType.USAGE, data=dict(usage))
        yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "stop"})
    return fake_invoke_stream


def _stream_tool_call(call_id: str, name: str, arguments: str, usage: dict = None):
    """生成一个返回 tool_call 的 fake invoke_stream。"""
    if usage is None:
        usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    def fake_invoke_stream(messages, tools=None, **kwargs):
        yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
            "index": 0, "id": call_id, "name": name, "arguments": arguments,
        })
        yield ModelEvent(type=ModelEventType.USAGE, data=dict(usage))
        yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "tool_calls"})
    return fake_invoke_stream


# ----------------------------------------------------------------
#  1. 纯文本回复
# ----------------------------------------------------------------
def test_e2e_plain_text_reply():
    """模型直接返回文本, 不调用工具 → done 事件 + Session 持久化。"""
    agent = _make_e2e_agent()
    agent.model_invoker.invoke_stream = _stream_text("hello world")
    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="hi")

    events = list(agent._stream_ai_loop(msg))
    types = [e["type"] for e in events]

    assert "token" in types
    assert "done" in types
    assert "error" not in types

    # Session 消息顺序: user -> assistant(final)
    roles = [c.args[1] for c in agent.session_mgr.add_message.call_args_list]
    assert roles == ["user", "assistant"]

    # memory.after_reply 被调用 (最终正常回复)
    agent.memory.after_reply.assert_called_once()
    args = agent.memory.after_reply.call_args.args
    assert args[3] == "hello world"  # bot_reply

    # token 记账一次
    agent.session_mgr.log_api_usage.assert_called_once()


# ----------------------------------------------------------------
#  2. 工具循环 (tool_call → tool_result → 最终文本)
# ----------------------------------------------------------------
def test_e2e_tool_loop():
    """模型先调用工具, 再返回最终文本。"""
    agent = _make_e2e_agent()
    # 第一步: tool_call; 第二步: 文本
    step = [0]
    def fake_invoke_stream(messages, tools=None, **kwargs):
        step[0] += 1
        if step[0] == 1:
            yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
                "index": 0, "id": "call_1", "name": "test_echo",
                "arguments": '{"text": "hi"}',
            })
            yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "tool_calls"})
        else:
            yield ModelEvent(type=ModelEventType.TEXT, data="final answer")
            yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "stop"})
    agent.model_invoker.invoke_stream = fake_invoke_stream

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="echo hi")
    events = list(agent._stream_ai_loop(msg))
    types = [e["type"] for e in events]

    # 事件顺序: token(tools 阶段无 token) -> tool_start -> tool_result -> token -> done
    assert "tool_start" in types
    assert "tool_result" in types
    assert "done" in types
    assert "error" not in types

    # Session 消息顺序: user -> assistant(tool_calls) -> tool -> assistant(final)
    roles = [c.args[1] for c in agent.session_mgr.add_message.call_args_list]
    assert roles == ["user", "assistant", "tool", "assistant"]

    # assistant(tool_calls) 消息包含 tool_calls_data
    assistant_calls = agent.session_mgr.add_message.call_args_list[1]
    assert assistant_calls.kwargs.get("tool_calls_data") is not None
    assert assistant_calls.kwargs["tool_calls_data"][0]["function"]["name"] == "test_echo"

    # tool 消息的 tool_call_id 关联
    tool_call = agent.session_mgr.add_message.call_args_list[2]
    assert tool_call.kwargs.get("tool_call_id") == "call_1"
    assert tool_call.kwargs.get("name") == "test_echo"

    # token 记账两次 (两步)
    assert agent.session_mgr.log_api_usage.call_count == 2

    # memory.after_reply 只在最终回复调用一次
    agent.memory.after_reply.assert_called_once()


# ----------------------------------------------------------------
#  3. 访客拒绝 (Guest 不能调用非 guest_ok 工具)
# ----------------------------------------------------------------
def test_e2e_guest_denied():
    """Guest 调用非 guest_ok 工具 → 权限拒绝 (ok=False)。"""
    agent = _make_e2e_agent()

    def fake_invoke_stream(messages, tools=None, **kwargs):
        yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
            "index": 0, "id": "call_1", "name": "test_echo",  # 非 guest_ok
            "arguments": '{"text": "hi"}',
        })
        yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8})
        yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "tool_calls"})
    agent.model_invoker.invoke_stream = fake_invoke_stream

    msg = IncomingMessage(channel="test", user_id="g1", chat_id="c1",
                          message_id="m1", text="echo hi", is_guest=True)
    events = list(agent._stream_ai_loop(msg))
    types = [e["type"] for e in events]

    # 工具被拒绝 (ok=False), 由于 max_steps=3 会循环多次, 最终 MAX_STEPS 终止
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) >= 1
    # 每次拒绝都标记 ok=False
    for tr in tool_results:
        assert tr["ok"] is False
        assert "权限" in tr["result"] or "无权" in tr["result"]

    # 工具结果已写入 Session, 内容包含权限拒绝信息
    tool_msgs = [c for c in agent.session_mgr.add_message.call_args_list
                 if c.args[1] == "tool"]
    assert len(tool_msgs) >= 1
    assert "无权" in tool_msgs[0].args[2] or "权限" in tool_msgs[0].args[2]

    # Guest 不调用 after_reply (最终是 MAX_STEPS 终止, 不是正常回复)
    agent.memory.after_reply.assert_not_called()


# ----------------------------------------------------------------
#  4. 流式重试 (首字前超时 → 自动重试 → 成功)
# ----------------------------------------------------------------
def test_e2e_stream_retry():
    """首字未生成时 timeout → 自动重试 → 成功。"""
    agent = _make_e2e_agent()
    call_count = [0]

    def fake_invoke_stream(messages, tools=None, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise openai.APITimeoutError("timeout")
        # 第二次成功
        yield ModelEvent(type=ModelEventType.TEXT, data="recovered")
        yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8})
        yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "stop"})

    agent.model_invoker.invoke_stream = fake_invoke_stream
    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="hi")
    events = list(agent._stream_ai_loop(msg))
    types = [e["type"] for e in events]

    # 重试了一次 (共 2 次调用)
    assert call_count[0] == 2
    assert "token" in types
    assert "done" in types
    assert "error" not in types

    # token 只记账一次 (重试不重复计费)
    agent.session_mgr.log_api_usage.assert_called_once()


# ----------------------------------------------------------------
#  5. 流式不重试 (已有输出后禁止重试)
# ----------------------------------------------------------------
def test_e2e_no_retry_after_output():
    """已产出 TEXT 后异常 → 不重试, 直接 ERROR。"""
    agent = _make_e2e_agent()

    def fake_invoke_stream(messages, tools=None, **kwargs):
        yield ModelEvent(type=ModelEventType.TEXT, data="partial")
        raise openai.APIConnectionError("connection reset")

    agent.model_invoker.invoke_stream = fake_invoke_stream
    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="hi")
    events = list(agent._stream_ai_loop(msg))
    types = [e["type"] for e in events]

    # 有 token (部分输出), 然后 error
    assert "token" in types
    assert "error" in types
    assert "done" in types


# ----------------------------------------------------------------
#  6. Token 记账 (每步只记账一次)
# ----------------------------------------------------------------
def test_e2e_token_accounting_once_per_step():
    """两步执行, 每步只调用一次 log_api_usage。"""
    agent = _make_e2e_agent()
    step = [0]
    def fake_invoke_stream(messages, tools=None, **kwargs):
        step[0] += 1
        if step[0] == 1:
            yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
                "index": 0, "id": "c1", "name": "test_echo",
                "arguments": '{"text":"x"}',
            })
            yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "tool_calls"})
        else:
            yield ModelEvent(type=ModelEventType.TEXT, data="done")
            yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "stop"})
    agent.model_invoker.invoke_stream = fake_invoke_stream

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="hi")
    list(agent._stream_ai_loop(msg))

    assert agent.session_mgr.log_api_usage.call_count == 2
    # 第二步的 total_tokens 应为 30 (不是累计 45)
    second_call = agent.session_mgr.log_api_usage.call_args_list[1]
    assert second_call.args[4] == 30  # total_tokens 参数


# ----------------------------------------------------------------
#  7. Session 消息顺序 (完整 tool loop)
# ----------------------------------------------------------------
def test_e2e_session_message_order():
    """验证 Session 消息顺序符合 OpenAI 协议:
    user -> assistant(tool_calls) -> tool -> ... -> assistant(final)
    """
    agent = _make_e2e_agent()
    step = [0]
    def fake_invoke_stream(messages, tools=None, **kwargs):
        step[0] += 1
        if step[0] == 1:
            yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
                "index": 0, "id": "call_a", "name": "test_echo",
                "arguments": '{"text":"a"}',
            })
            yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "tool_calls"})
        else:
            yield ModelEvent(type=ModelEventType.TEXT, data="all done")
            yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "stop"})
    agent.model_invoker.invoke_stream = fake_invoke_stream

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="do it")
    list(agent._stream_ai_loop(msg))

    calls = agent.session_mgr.add_message.call_args_list
    # 严格顺序: user, assistant(tool_calls), tool, assistant(final)
    assert calls[0].args[1] == "user"
    assert calls[1].args[1] == "assistant"
    assert calls[1].kwargs.get("tool_calls_data") is not None
    assert calls[2].args[1] == "tool"
    assert calls[2].kwargs.get("tool_call_id") == "call_a"
    assert calls[3].args[1] == "assistant"
    assert calls[3].kwargs.get("tool_calls_data") is None


# ----------------------------------------------------------------
#  8. Memory 回调 (只在最终正常回复时调用)
# ----------------------------------------------------------------
def test_e2e_memory_callback_only_on_final_reply():
    """Memory.after_reply 只在最终正常回复时调用, 错误/工具中间结果不调用。"""
    agent = _make_e2e_agent()
    step = [0]
    def fake_invoke_stream(messages, tools=None, **kwargs):
        step[0] += 1
        if step[0] == 1:
            yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
                "index": 0, "id": "c1", "name": "test_echo",
                "arguments": '{"text":"x"}',
            })
            yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "tool_calls"})
        else:
            yield ModelEvent(type=ModelEventType.TEXT, data="final")
            yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "stop"})
    agent.model_invoker.invoke_stream = fake_invoke_stream

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="hi")
    list(agent._stream_ai_loop(msg))

    # after_reply 只调用一次 (最终回复), 不是每步都调用
    agent.memory.after_reply.assert_called_once()
    # before_reply 调用一次
    agent.memory.before_reply.assert_called_once()


# ----------------------------------------------------------------
#  9. 终止事件: ERROR
# ----------------------------------------------------------------
def test_e2e_terminate_on_error():
    """模型调用异常 → ERROR 事件 + DONE, 不调用 after_reply。"""
    agent = _make_e2e_agent()
    def fake_invoke_stream(messages, tools=None, **kwargs):
        raise openai.BadRequestError("bad request", response=MagicMock(), body=None)
    agent.model_invoker.invoke_stream = fake_invoke_stream

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="hi")
    events = list(agent._stream_ai_loop(msg))
    types = [e["type"] for e in events]

    assert "error" in types
    assert "done" in types
    # 错误时不调用 after_reply
    agent.memory.after_reply.assert_not_called()


# ----------------------------------------------------------------
#  10. 终止事件: DEAD_LOOP
# ----------------------------------------------------------------
def test_e2e_terminate_on_dead_loop():
    """死循环检测触发 → DEAD_LOOP 事件 + 不调用 after_reply。"""
    agent = _make_e2e_agent()
    # 每步都调用相同 tool + 相同 arguments -> 触发 LoopDetector
    def fake_invoke_stream(messages, tools=None, **kwargs):
        yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
            "index": 0, "id": "c1", "name": "test_echo",
            "arguments": '{"text":"same"}',
        })
        yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "tool_calls"})
    agent.model_invoker.invoke_stream = fake_invoke_stream

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="loop")
    events = list(agent._stream_ai_loop(msg))
    types = [e["type"] for e in events]

    assert "error" in types
    assert "done" in types
    # 死循环不调用 after_reply
    agent.memory.after_reply.assert_not_called()
    # mark_done 被调用
    agent.session_mgr.mark_done.assert_called()


# ----------------------------------------------------------------
#  11. 终止事件: MAX_STEPS
# ----------------------------------------------------------------
def test_e2e_terminate_on_max_steps():
    """超出 max_steps → MAX_STEPS 事件, 不调用 after_reply。"""
    agent = _make_e2e_agent()
    # 每步调用不同 arguments 避免死循环, 但永不返回最终文本
    counter = [0]
    def fake_invoke_stream(messages, tools=None, **kwargs):
        counter[0] += 1
        yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
            "index": 0, "id": f"c{counter[0]}", "name": "test_echo",
            "arguments": json.dumps({"text": str(counter[0])}),
        })
        yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "tool_calls"})
    agent.model_invoker.invoke_stream = fake_invoke_stream

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="loop forever")
    events = list(agent._stream_ai_loop(msg))
    types = [e["type"] for e in events]

    assert "error" in types
    assert "done" in types
    # max_steps 不调用 after_reply
    agent.memory.after_reply.assert_not_called()


# ----------------------------------------------------------------
#  12. 终止事件: TOKEN_BUDGET_EXCEEDED
# ----------------------------------------------------------------
def test_e2e_terminate_on_token_budget():
    """token_budget 超限 → TOKEN_BUDGET_EXCEEDED 事件, 不调用 after_reply。"""
    agent = _make_e2e_agent()
    # 设置很小的 token_budget
    from core.execution import ExecutionContext, ActorType, ExecutionSource
    # 通过 patch runtime.run 注入小预算 ctx
    # 更简单: 直接设置 ctx.token_budget
    orig_run = agent.runtime.run

    def patched_run(messages, tools, ctx, timeout=60.0, stream=True):
        from core.execution import ExecutionContext
        new_ctx = ExecutionContext(
            actor_id=ctx.actor_id,
            actor_type=ctx.actor_type,
            source=ctx.source,
            allowed_tools=ctx.allowed_tools,
            session_key=ctx.session_key,
            max_steps=ctx.max_steps,
            max_output_tokens=ctx.max_output_tokens,
            token_budget=5,  # 极小预算, 第一步就超
        )
        yield from orig_run(messages, tools, new_ctx, timeout=timeout, stream=stream)

    agent.runtime.run = patched_run

    agent.model_invoker.invoke_stream = _stream_text("hello", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="hi")
    events = list(agent._stream_ai_loop(msg))
    types = [e["type"] for e in events]

    assert "error" in types
    assert "done" in types
    agent.memory.after_reply.assert_not_called()


# ----------------------------------------------------------------
#  13. provider_metadata 保留 (TOOL_CALLS_READY 持久化)
# ----------------------------------------------------------------
def test_e2e_provider_metadata_preserved():
    """Gemini thought_signature 等 provider_metadata 必须持久化到 Session。"""
    agent = _make_e2e_agent()
    step = [0]
    def fake_invoke_stream(messages, tools=None, **kwargs):
        step[0] += 1
        if step[0] == 1:
            # 带 provider_metadata 的 tool_call
            yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
                "index": 0, "id": "call_x", "name": "test_echo",
                "arguments": '{"text":"hi"}',
                "provider_metadata": {"thought_signature": "sig_abc"},
            })
            yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "tool_calls"})
        else:
            yield ModelEvent(type=ModelEventType.TEXT, data="done")
            yield ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "stop"})
    agent.model_invoker.invoke_stream = fake_invoke_stream

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="hi")
    list(agent._stream_ai_loop(msg))

    # assistant(tool_calls) 消息应包含 provider_metadata
    assistant_call = agent.session_mgr.add_message.call_args_list[1]
    tool_calls_data = assistant_call.kwargs["tool_calls_data"]
    assert tool_calls_data[0].get("provider_metadata") == {"thought_signature": "sig_abc"}


# ----------------------------------------------------------------
#  14. Guest 使用 ActorType.GUEST
# ----------------------------------------------------------------
def test_e2e_guest_uses_guest_actor_type():
    """Guest 消息 → ExecutionContext.actor_type == GUEST。"""
    agent = _make_e2e_agent()
    captured_ctx = []
    orig_run = agent.runtime.run
    def patched_run(messages, tools, ctx, timeout=60.0, stream=True):
        captured_ctx.append(ctx)
        yield from orig_run(messages, tools, ctx, timeout=timeout, stream=stream)
    agent.runtime.run = patched_run
    agent.model_invoker.invoke_stream = _stream_text("hi")

    msg = IncomingMessage(channel="test", user_id="g1", chat_id="c1",
                          message_id="m1", text="hi", is_guest=True)
    list(agent._stream_ai_loop(msg))

    assert len(captured_ctx) == 1
    assert captured_ctx[0].actor_type.value == "guest"
    # Guest 的工具集被限制
    assert captured_ctx[0].allowed_tools is not None
    assert "test_guest_ok" in captured_ctx[0].allowed_tools
    assert "test_echo" not in captured_ctx[0].allowed_tools


# ----------------------------------------------------------------
#  15. 日额度贯通: 初始未超限，第一步 usage 后超额 → 终止
# ----------------------------------------------------------------
def test_e2e_quota_breach_after_first_step():
    """初始额度未超限，但第一步 usage 超过剩余额度：
       不得进入第二轮、不得执行本轮工具。
    """
    agent = _make_e2e_agent()
    agent.daily_token_limit = 10  # 极小额度

    mock_session = agent.session_mgr.get_or_create.return_value
    mock_session.token_usage = 0  # 初始未超
    mock_session.status = "working"

    # log_api_usage 的 side_effect: 累加 token_usage 到 mock_session
    def fake_log_usage(session_key, model, pt, ct, tt, provider=None, estimated=False):
        mock_session.token_usage += tt
    agent.session_mgr.log_api_usage = MagicMock(side_effect=fake_log_usage)

    # 第一步: tool_call (但有 usage), Runtime 会尝试进入 TOOL_CALLS_READY
    # 但 Agent 应在 STEP_END 检查到超额后立即终止
    call_count = [0]
    def fake_invoke_stream(messages, tools=None, **kwargs):
        call_count[0] += 1
        # 第一步只发 tool_call_delta + usage (不发 DONE, 因为 Agent 会先终止)
        # 但 Runtime 的 _consume_stream 会消费完整个流, 所以需要发 DONE
        if call_count[0] == 1:
            yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
                "index": 0, "id": "c1", "name": "test_echo",
                "arguments": '{"text":"x"}',
            })
            # usage 20 tokens > 剩余额度 10
            yield ModelEvent(type=ModelEventType.USAGE, data={
                "prompt_tokens": 15, "completion_tokens": 5, "total_tokens": 20})
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "tool_calls"})
        else:
            # 不应到达第二轮
            yield ModelEvent(type=ModelEventType.TEXT, data="should not reach")
            yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "stop"})
    agent.model_invoker.invoke_stream = fake_invoke_stream

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="hi")
    events = list(agent._stream_ai_loop(msg))
    types = [e["type"] for e in events]

    # 终止: error (额度耗尽) + done
    assert "error" in types
    assert "done" in types

    # 不得进入第二轮 (invoke_stream 只被调用一次)
    assert call_count[0] == 1

    # 不得执行本轮工具 (无 tool_start 事件)
    assert "tool_start" not in types
    assert "tool_result" not in types

    # mark_done 被调用 (会话从 working -> done)
    agent.session_mgr.mark_done.assert_called_once()

    # after_reply 不调用 (不是正常回复)
    agent.memory.after_reply.assert_not_called()


# ----------------------------------------------------------------
#  16. Provider 不返回 usage: 估算包含每步实际 TEXT/REASONING
# ----------------------------------------------------------------
def test_e2e_estimate_includes_step_output():
    """Provider 不返回 USAGE 时，本地估算必须包含本步实际 TEXT/REASONING 输出。"""
    agent = _make_e2e_agent()

    # 估算的 completion_tokens 与文本长度正相关, 用很长的文本验证非零
    long_text = "A" * 500  # 足够长, 使 _estimate_tokens 的 completion > 0

    def fake_invoke_stream(messages, tools=None, **kwargs):
        # 不发 USAGE 事件, 触发估算兜底
        yield ModelEvent(type=ModelEventType.TEXT, data=long_text)
        yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": "stop"})

    agent.model_invoker.invoke_stream = fake_invoke_stream

    captured_usage = []
    def capture_log_usage(session_key, model, pt, ct, tt, provider=None, estimated=False):
        captured_usage.append({"pt": pt, "ct": ct, "tt": tt, "estimated": estimated})
    agent.session_mgr.log_api_usage = MagicMock(side_effect=capture_log_usage)

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="hi")
    list(agent._stream_ai_loop(msg))

    # 只一步
    assert len(captured_usage) == 1
    step = captured_usage[0]
    assert step["estimated"] is True
    # completion_tokens > 0 (估算包含了本步 TEXT 输出)
    assert step["ct"] > 0, "估算的 completion_tokens 必须包含本步 TEXT 输出"
    # total = prompt + completion
    assert step["tt"] == step["pt"] + step["ct"]


# ----------------------------------------------------------------
#  17. Runtime ERROR: Session 写入错误终态，working → done
# ----------------------------------------------------------------
def test_e2e_error_writes_session_terminal_state():
    """模型异常 → ERROR 事件 + Session 写入错误消息 + mark_done + 不调用 after_reply。"""
    agent = _make_e2e_agent()
    mock_session = agent.session_mgr.get_or_create.return_value
    mock_session.status = "working"

    def fake_invoke_stream(messages, tools=None, **kwargs):
        raise openai.BadRequestError("bad request", response=MagicMock(), body=None)
    agent.model_invoker.invoke_stream = fake_invoke_stream

    msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1",
                          message_id="m1", text="hi")
    events = list(agent._stream_ai_loop(msg))
    types = [e["type"] for e in events]

    assert "error" in types
    assert "done" in types

    # Session 写入错误消息 (assistant 角色, 内容含 ❌)
    calls = agent.session_mgr.add_message.call_args_list
    # 最后一条 assistant 消息应为错误终态
    assistant_msgs = [c for c in calls if c.args[1] == "assistant"]
    assert len(assistant_msgs) >= 1
    error_msg = assistant_msgs[-1].args[2]
    assert "❌" in error_msg or "bad request" in error_msg.lower()

    # mark_done 被调用 (working -> done)
    agent.session_mgr.mark_done.assert_called_once()

    # after_reply 不调用
    agent.memory.after_reply.assert_not_called()
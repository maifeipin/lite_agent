"""Phase 2b — ModelEvent / ModelInvoker 独立测试

覆盖 OpenAI 流式/同步、Gemini 同步、参数组装、协议转换、异常处理、429 重试、
安全过滤，以及 ModelEvent 深度不可变性与类型枚举。
"""

import json
import pytest
import sys
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch, PropertyMock

import openai

from core.model_event import ModelEvent, ModelEventType
from core.model_invoker import OpenAIInvoker, GeminiInvoker


# ================================================================
#  OpenAIInvoker — 流式调用
# ================================================================

def _make_stream_chunk(content=None, reasoning=None, tool_calls=None,
                       usage=None, finish_reason=None):
    """构造一个与 OpenAI 流式 chunk 兼容的假对象。"""
    delta_attrs = {}
    if content is not None:
        delta_attrs["content"] = content
    if reasoning is not None:
        delta_attrs["reasoning_content"] = reasoning
    if tool_calls is not None:
        delta_attrs["tool_calls"] = tool_calls
    delta = SimpleNamespace(**delta_attrs)
    choice = SimpleNamespace(finish_reason=finish_reason, delta=delta)
    return SimpleNamespace(choices=[choice], usage=usage)


def _make_usage_chunk(prompt=100, completion=50, total=150):
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)
    return SimpleNamespace(choices=[], usage=usage)


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __enter__(self):
        return iter(self._chunks)

    def __exit__(self, *args):
        pass


@pytest.fixture
def openai_invoker():
    client = MagicMock()
    return OpenAIInvoker(client=client, model_name="test-model", temperature=0.3, max_tokens=256)


class TestOpenAIStream:
    """流式调用：text / reasoning / usage / done / tool_call_delta / error"""

    def test_text_event(self, openai_invoker):
        chunks = [
            _make_stream_chunk(content="Hello"),
            _make_stream_chunk(content=" World"),
            _make_stream_chunk(finish_reason="stop"),
        ]
        openai_invoker.client.chat.completions.create.return_value = FakeStream(chunks)

        events = list(openai_invoker.invoke_stream([{"role": "user", "content": "hi"}]))

        text_values = [e.data for e in events if e.type == ModelEventType.TEXT]
        assert text_values == ["Hello", " World"]
        assert any(e.type == ModelEventType.DONE for e in events)

    def test_reasoning_event(self, openai_invoker):
        chunks = [
            _make_stream_chunk(reasoning="Let me think..."),
            _make_stream_chunk(reasoning=" about this"),
            _make_stream_chunk(content="Answer"),
            _make_stream_chunk(finish_reason="stop"),
        ]
        openai_invoker.client.chat.completions.create.return_value = FakeStream(chunks)

        events = list(openai_invoker.invoke_stream([{"role": "user", "content": "q"}]))

        reasoning_values = [e.data for e in events if e.type == ModelEventType.REASONING]
        assert reasoning_values == ["Let me think...", " about this"]
        assert any(e.type == ModelEventType.TEXT for e in events)
        assert any(e.type == ModelEventType.DONE for e in events)

    def test_usage_event(self, openai_invoker):
        chunks = [
            _make_stream_chunk(content="Hi"),
            _make_usage_chunk(prompt=10, completion=5, total=15),
            _make_stream_chunk(finish_reason="stop"),
        ]
        openai_invoker.client.chat.completions.create.return_value = FakeStream(chunks)

        events = list(openai_invoker.invoke_stream([{"role": "user", "content": "hi"}]))

        usage_events = [e for e in events if e.type == ModelEventType.USAGE]
        assert len(usage_events) == 1
        assert usage_events[0].data == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_done_finish_reason(self, openai_invoker):
        chunks = [
            _make_stream_chunk(content="Done"),
            _make_stream_chunk(finish_reason="length"),
        ]
        openai_invoker.client.chat.completions.create.return_value = FakeStream(chunks)

        events = list(openai_invoker.invoke_stream([{"role": "user", "content": "hi"}]))

        done_events = [e for e in events if e.type == ModelEventType.DONE]
        assert len(done_events) == 1
        assert done_events[0].data["finish_reason"] == "length"

    def test_stream_exception_yields_error(self, openai_invoker):
        openai_invoker.client.chat.completions.create.side_effect = openai.APITimeoutError("timeout")

        events = list(openai_invoker.invoke_stream([{"role": "user", "content": "hi"}]))

        error_events = [e for e in events if e.type == ModelEventType.ERROR]
        assert len(error_events) == 1
        assert isinstance(error_events[0].data, openai.APITimeoutError)
        assert not any(e.type == ModelEventType.DONE for e in events)

    # ---- 多工具、多分片 arguments 累积 ----

    def _make_tc_delta(self, index, id=None, name=None, arguments=None):
        """构造一个 tool_call delta 对象。"""
        fn_attrs = {}
        if name is not None:
            fn_attrs["name"] = name
        if arguments is not None:
            fn_attrs["arguments"] = arguments
        fn = SimpleNamespace(**fn_attrs) if fn_attrs else None
        tc = SimpleNamespace(index=index, id=id, function=fn)
        return tc

    def test_single_tool_call_delta_accumulation(self, openai_invoker):
        """单工具多次分片 → arguments 累积拼接。"""
        chunks = [
            _make_stream_chunk(tool_calls=[
                self._make_tc_delta(0, id="call_1", name="get_weather", arguments='{"city":'),
            ]),
            _make_stream_chunk(tool_calls=[
                self._make_tc_delta(0, arguments='"Beijing"}'),
            ]),
            _make_stream_chunk(finish_reason="tool_calls"),
        ]
        openai_invoker.client.chat.completions.create.return_value = FakeStream(chunks)

        events = list(openai_invoker.invoke_stream([{"role": "user", "content": "weather"}]))

        tc_events = [e for e in events if e.type == ModelEventType.TOOL_CALL_DELTA]
        assert len(tc_events) == 2
        assert tc_events[0].data["id"] == "call_1"
        assert tc_events[0].data["name"] == "get_weather"
        assert tc_events[0].data["arguments"] == '{"city":'
        assert tc_events[1].data["arguments"] == '"Beijing"}'

    def test_multi_tool_call_delta_accumulation(self, openai_invoker):
        """两个工具并行调用，分片交错 → 各自累积。"""
        chunks = [
            _make_stream_chunk(tool_calls=[
                self._make_tc_delta(0, id="call_a", name="search", arguments='{"q":'),
                self._make_tc_delta(1, id="call_b", name="fetch", arguments='{"url":'),
            ]),
            _make_stream_chunk(tool_calls=[
                self._make_tc_delta(0, arguments='"test"}'),
                self._make_tc_delta(1, arguments='"http://x"}'),
            ]),
            _make_stream_chunk(finish_reason="tool_calls"),
        ]
        openai_invoker.client.chat.completions.create.return_value = FakeStream(chunks)

        events = list(openai_invoker.invoke_stream([{"role": "user", "content": "do"}]))

        tc_events = [e for e in events if e.type == ModelEventType.TOOL_CALL_DELTA]
        assert len(tc_events) == 4
        tc0_parts = [e.data["arguments"] for e in tc_events if e.data["index"] == 0]
        assert tc0_parts == ['{"q":', '"test"}']

    def test_tool_call_delta_id_name_only_first_chunk(self, openai_invoker):
        """id 和 name 只在第一个分片出现，后续分片为 None。"""
        chunks = [
            _make_stream_chunk(tool_calls=[
                self._make_tc_delta(0, id="call_1", name="f"),
            ]),
            _make_stream_chunk(tool_calls=[
                self._make_tc_delta(0, arguments='{"x":1}'),
            ]),
            _make_stream_chunk(finish_reason="tool_calls"),
        ]
        openai_invoker.client.chat.completions.create.return_value = FakeStream(chunks)

        events = list(openai_invoker.invoke_stream([{"role": "user", "content": "x"}]))

        tc_events = [e for e in events if e.type == ModelEventType.TOOL_CALL_DELTA]
        assert tc_events[0].data["id"] == "call_1"
        assert tc_events[0].data["name"] == "f"
        assert tc_events[1].data.get("id") is None
        assert tc_events[1].data.get("name") is None


# ================================================================
#  OpenAIInvoker — 同步调用
# ================================================================

class TestOpenAISync:
    """同步调用：文本、tool calls、usage、参数"""

    @pytest.fixture
    def invoker(self):
        client = MagicMock()
        return OpenAIInvoker(client=client, model_name="test-model", temperature=0.3, max_tokens=256)

    def _set_response(self, invoker, content="", tool_calls=None, finish_reason="stop",
                      total_tokens=100):
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        choice = SimpleNamespace(finish_reason=finish_reason, message=msg)
        usage = SimpleNamespace(total_tokens=total_tokens)
        response = SimpleNamespace(choices=[choice], usage=usage)
        invoker.client.chat.completions.create.return_value = response

    def _make_tc(self, id, name, arguments):
        fn = SimpleNamespace(name=name, arguments=arguments)
        return SimpleNamespace(id=id, function=fn)

    def test_text_response(self, invoker):
        self._set_response(invoker, content="Hello, world!")
        result = invoker.invoke_sync([{"role": "user", "content": "hi"}])
        assert result["content"] == "Hello, world!"
        assert result["tool_calls"] == []
        assert result["finish_reason"] == "stop"
        assert result["empty"] is False

    def test_single_tool_call(self, invoker):
        self._set_response(
            invoker,
            finish_reason="tool_calls",
            tool_calls=[self._make_tc("call_1", "get_weather", '{"city":"Beijing"}')],
        )
        result = invoker.invoke_sync([{"role": "user", "content": "weather"}])
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["id"] == "call_1"
        assert result["tool_calls"][0]["name"] == "get_weather"
        assert result["tool_calls"][0]["arguments"] == '{"city":"Beijing"}'

    def test_multiple_tool_calls(self, invoker):
        self._set_response(
            invoker,
            finish_reason="tool_calls",
            tool_calls=[
                self._make_tc("call_a", "search", '{"q":"x"}'),
                self._make_tc("call_b", "fetch", '{"url":"y"}'),
            ],
        )
        result = invoker.invoke_sync([{"role": "user", "content": "do"}])
        assert len(result["tool_calls"]) == 2
        assert result["tool_calls"][0]["name"] == "search"
        assert result["tool_calls"][1]["name"] == "fetch"

    def test_usage_total(self, invoker):
        self._set_response(invoker, content="ok", total_tokens=42)
        result = invoker.invoke_sync([{"role": "user", "content": "hi"}])
        assert result["usage_total"] == 42

    def test_usage_none(self, invoker):
        """usage 为 None 时返回 0。"""
        msg = SimpleNamespace(content="ok", tool_calls=None)
        choice = SimpleNamespace(finish_reason="stop", message=msg)
        response = SimpleNamespace(choices=[choice], usage=None)
        invoker.client.chat.completions.create.return_value = response
        result = invoker.invoke_sync([{"role": "user", "content": "hi"}])
        assert result["usage_total"] == 0

    def test_empty_content_becomes_empty_str(self, invoker):
        """content 为 None 时返回空字符串。"""
        self._set_response(invoker, content=None)
        result = invoker.invoke_sync([{"role": "user", "content": "hi"}])
        assert result["content"] == ""

    # ---- 请求参数 ----

    def test_normal_model_kwargs(self, invoker):
        self._set_response(invoker, content="ok")
        invoker.invoke_sync([{"role": "user", "content": "hi"}])
        call_kwargs = invoker.client.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "test-model"
        assert call_kwargs["temperature"] == 0.3
        assert call_kwargs["max_tokens"] == 256
        assert "reasoning_effort" not in call_kwargs
        assert "extra_body" not in call_kwargs

    def test_pro_model_kwargs(self):
        invoker = OpenAIInvoker(client=MagicMock(), model_name="pro-model", temperature=0.3, max_tokens=256)
        msg = SimpleNamespace(content="ok", tool_calls=None)
        choice = SimpleNamespace(finish_reason="stop", message=msg)
        response = SimpleNamespace(choices=[choice], usage=SimpleNamespace(total_tokens=10))
        invoker.client.chat.completions.create.return_value = response

        invoker.invoke_sync([{"role": "user", "content": "hi"}])
        call_kwargs = invoker.client.chat.completions.create.call_args[1]
        assert call_kwargs["reasoning_effort"] == "high"
        assert call_kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
        assert "temperature" not in call_kwargs
        assert "max_tokens" not in call_kwargs

    def test_reasoner_model_kwargs(self):
        invoker = OpenAIInvoker(client=MagicMock(), model_name="reasoner-v2", temperature=0.3, max_tokens=256)
        msg = SimpleNamespace(content="ok", tool_calls=None)
        choice = SimpleNamespace(finish_reason="stop", message=msg)
        response = SimpleNamespace(choices=[choice], usage=SimpleNamespace(total_tokens=10))
        invoker.client.chat.completions.create.return_value = response

        invoker.invoke_sync([{"role": "user", "content": "hi"}])
        call_kwargs = invoker.client.chat.completions.create.call_args[1]
        assert call_kwargs["reasoning_effort"] == "high"

    # ---- tools 参数 ----

    def test_tools_none(self, invoker):
        self._set_response(invoker, content="ok")
        invoker.invoke_sync([{"role": "user", "content": "hi"}], tools=None)
        call_kwargs = invoker.client.chat.completions.create.call_args[1]
        assert "tools" not in call_kwargs
        assert "tool_choice" not in call_kwargs

    def test_tools_empty(self, invoker):
        self._set_response(invoker, content="ok")
        invoker.invoke_sync([{"role": "user", "content": "hi"}], tools=[])
        call_kwargs = invoker.client.chat.completions.create.call_args[1]
        assert "tools" not in call_kwargs

    def test_tools_non_empty(self, invoker):
        self._set_response(invoker, content="ok")
        tools = [{"type": "function", "function": {"name": "echo"}}]
        invoker.invoke_sync([{"role": "user", "content": "hi"}], tools=tools)
        call_kwargs = invoker.client.chat.completions.create.call_args[1]
        assert call_kwargs["tools"] == tools
        assert call_kwargs["tool_choice"] == "auto"

    # ---- 流式参数 ----

    def test_stream_kwargs_includes_stream_options(self, invoker):
        chunks = [_make_stream_chunk(content="Hi"), _make_stream_chunk(finish_reason="stop")]
        invoker.client.chat.completions.create.return_value = FakeStream(chunks)
        list(invoker.invoke_stream([{"role": "user", "content": "hi"}]))
        call_kwargs = invoker.client.chat.completions.create.call_args[1]
        assert call_kwargs["stream"] is True
        assert call_kwargs["stream_options"] == {"include_usage": True}

    def test_stream_timeout_override(self, invoker):
        chunks = [_make_stream_chunk(content="Hi"), _make_stream_chunk(finish_reason="stop")]
        invoker.client.chat.completions.create.return_value = FakeStream(chunks)
        list(invoker.invoke_stream([{"role": "user", "content": "hi"}], timeout=120.0))
        call_kwargs = invoker.client.chat.completions.create.call_args[1]
        assert call_kwargs["timeout"] == 120.0


# ================================================================
#  GeminiInvoker — 同步调用
# ================================================================

class TestGeminiSync:
    """Gemini 同步：消息转换、工具声明、429 重试、安全过滤"""

    @pytest.fixture
    def gemini_invoker(self):
        client = MagicMock()
        return GeminiInvoker(
            client=client,
            model_name="gemini-pro",
            temperature=0.3,
            max_tokens=256,
        )

    def _setup_gemini_patches(self):
        """mock google.genai.types 和 gemini_codec 调用。

        使用 SimpleNamespace 构造假模块注入 sys.modules，并设置 __path__/__spec__
        防止 Python 尝试文件系统导入。
        """
        if "google" not in sys.modules:
            exc_mod = SimpleNamespace(__name__="google.api_core.exceptions")
            types_mod = SimpleNamespace(
                __name__="google.genai.types",
                Tool=MagicMock(return_value=MagicMock()),
                GenerateContentConfig=MagicMock(return_value=MagicMock()),
            )
            genai_mod = SimpleNamespace(
                __name__="google.genai",
                __path__=[],
                __spec__=SimpleNamespace(submodule_search_locations=[]),
                types=types_mod,
            )
            api_core_mod = SimpleNamespace(
                __name__="google.api_core",
                __path__=[],
                __spec__=SimpleNamespace(submodule_search_locations=[]),
                exceptions=exc_mod,
            )
            google_mod = SimpleNamespace(
                __name__="google",
                __path__=[],
                __spec__=SimpleNamespace(submodule_search_locations=[]),
                api_core=api_core_mod,
                genai=genai_mod,
            )

            sys.modules["google"] = google_mod
            sys.modules["google.api_core"] = api_core_mod
            sys.modules["google.api_core.exceptions"] = exc_mod
            sys.modules["google.genai"] = genai_mod
            sys.modules["google.genai.types"] = types_mod
            self._google_injected = True
        else:
            self._google_injected = False

        return [
            patch("core.gemini_codec.openai_messages_to_gemini",
                  return_value=("sys", [{"role": "user", "parts": [{"text": "hi"}]}])),
            patch("core.gemini_codec.gemini_response_to_unified",
                  return_value={"content": "ok", "tool_calls": [], "finish_reason": "STOP", "usage_total": 10, "empty": False}),
        ]

    def test_message_conversion_called(self, gemini_invoker):
        """invoke_sync 应调用 openai_messages_to_gemini 转换消息。"""
        patches = self._setup_gemini_patches()
        for p in patches:
            p.start()
        try:
            gemini_invoker.invoke_sync([{"role": "user", "content": "hi"}])
        finally:
            for p in reversed(patches):
                p.stop()

    def test_tools_passed_to_gemini_declarations(self, gemini_invoker):
        """传入 tools (OpenAI 格式) 时应通过 openai_tools_to_gemini_declarations 转换。"""
        patches = self._setup_gemini_patches()
        for p in patches:
            p.start()
        try:
            tools = [
                {"type": "function", "function": {"name": "echo", "description": "Echo",
                    "parameters": {"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]}}},
                {"type": "function", "function": {"name": "search", "description": "Search",
                    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}},
            ]
            with patch("core.gemini_codec.openai_tools_to_gemini_declarations") as mock_conv:
                mock_conv.return_value = [{"name": "echo"}, {"name": "search"}]
                gemini_invoker.invoke_sync([{"role": "user", "content": "hi"}], tools=tools)
                mock_conv.assert_called_once_with(tools)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_tools_none_no_tool_config(self, gemini_invoker):
        """tools=None 时不发送工具声明。"""
        patches = self._setup_gemini_patches()
        for p in patches:
            p.start()
        try:
            with patch("core.gemini_codec.openai_tools_to_gemini_declarations") as mock_conv:
                gemini_invoker.invoke_sync([{"role": "user", "content": "hi"}], tools=None)
                mock_conv.assert_not_called()
        finally:
            for p in reversed(patches):
                p.stop()

    def test_tools_empty_no_tool_config(self, gemini_invoker):
        """tools=[] 时不发送工具声明。"""
        patches = self._setup_gemini_patches()
        for p in patches:
            p.start()
        try:
            with patch("core.gemini_codec.openai_tools_to_gemini_declarations") as mock_conv:
                gemini_invoker.invoke_sync([{"role": "user", "content": "hi"}], tools=[])
                mock_conv.assert_not_called()
        finally:
            for p in reversed(patches):
                p.stop()

    def test_429_retry_then_success(self, gemini_invoker):
        """429 触发重试，第三次成功。"""
        ResourceExhausted = type("ResourceExhausted", (Exception,), {})

        call_count = [0]

        def mock_generate(**kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise ResourceExhausted("429 Resource has been exhausted")
            return MagicMock()

        gemini_invoker.client.models.generate_content = mock_generate

        patches = self._setup_gemini_patches()
        patches.append(patch("core.model_invoker.time.sleep"))  # 跳过 35s 等待
        for p in patches:
            p.start()
        try:
            gemini_invoker.invoke_sync([{"role": "user", "content": "hi"}])
            assert call_count[0] == 3
        finally:
            for p in reversed(patches):
                p.stop()

    def test_429_exhausts_retries(self, gemini_invoker):
        """429 重试 3 次全部失败 → 抛出异常。"""
        ResourceExhausted = type("ResourceExhausted", (Exception,), {})

        call_count = [0]

        def mock_generate(**kwargs):
            call_count[0] += 1
            raise ResourceExhausted("429 Resource has been exhausted")

        gemini_invoker.client.models.generate_content = mock_generate

        patches = self._setup_gemini_patches()
        patches.append(patch("core.model_invoker.time.sleep"))  # 跳过 35s 等待
        for p in patches:
            p.start()
        try:
            with pytest.raises(ResourceExhausted):
                gemini_invoker.invoke_sync([{"role": "user", "content": "hi"}])
            assert call_count[0] == 3
        finally:
            for p in reversed(patches):
                p.stop()

    def test_non_429_exception_not_retried(self, gemini_invoker):
        """非 429 异常不重试，直接抛出。"""
        call_count = [0]

        def mock_generate(**kwargs):
            call_count[0] += 1
            raise ValueError("some other error")

        gemini_invoker.client.models.generate_content = mock_generate

        patches = self._setup_gemini_patches()
        for p in patches:
            p.start()
        try:
            with pytest.raises(ValueError):
                gemini_invoker.invoke_sync([{"role": "user", "content": "hi"}])
            assert call_count[0] == 1
        finally:
            for p in reversed(patches):
                p.stop()

    def test_gemini_response_unified_called(self, gemini_invoker):
        """Gemini 响应应经过 gemini_response_to_unified 转换。"""
        patches = self._setup_gemini_patches()
        for p in patches:
            p.start()
        try:
            result = gemini_invoker.invoke_sync([{"role": "user", "content": "hi"}])
            assert result["content"] == "ok"
        finally:
            for p in reversed(patches):
                p.stop()

    def test_invoke_stream_returns_not_implemented(self, gemini_invoker):
        """Gemini 流式尚未实现，应返回 error 事件。"""
        events = list(gemini_invoker.invoke_stream([{"role": "user", "content": "hi"}]))
        assert len(events) == 1
        assert events[0].type == ModelEventType.ERROR
        assert isinstance(events[0].data, NotImplementedError)


# ================================================================
#  Gemini schema 转换 — 纯格式转换
# ================================================================

class TestGeminiToolSchemaConversion:
    """openai_tools_to_gemini_declarations 纯格式转换。"""

    def _conv(self):
        from core.gemini_codec import openai_tools_to_gemini_declarations
        return openai_tools_to_gemini_declarations

    def test_empty_tools(self):
        assert self._conv()([]) == []

    def test_single_tool_basic(self):
        tools = [{"type": "function", "function": {"name": "echo", "description": "Echo",
            "parameters": {"type": "object", "properties": {}, "required": []}}}]
        decls = self._conv()(tools)
        assert len(decls) == 1
        assert decls[0]["name"] == "echo"
        assert decls[0]["description"] == "Echo"
        assert decls[0]["parameters"]["type"] == "OBJECT"

    def test_parameter_types_uppercased(self):
        tools = [{"type": "function", "function": {"name": "f", "description": "d",
            "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}}]
        decls = self._conv()(tools)
        assert decls[0]["parameters"]["properties"]["x"]["type"] == "STRING"

    def test_enum_preserved(self):
        tools = [{"type": "function", "function": {"name": "f", "description": "d",
            "parameters": {"type": "object", "properties": {"x": {"type": "string", "enum": ["a", "b"]}}, "required": []}}}]
        decls = self._conv()(tools)
        assert decls[0]["parameters"]["properties"]["x"]["enum"] == ["a", "b"]


# ================================================================
#  ModelEvent — 深度不可变 + 类型枚举
# ================================================================

class TestModelEventImmutability:
    """ModelEvent 是 frozen dataclass + data/meta 深度不可变 + 类型枚举。"""

    def test_type_is_enum(self):
        e = ModelEvent(type=ModelEventType.TEXT, data="hello")
        assert e.type == ModelEventType.TEXT
        assert isinstance(e.type, ModelEventType)

    def test_type_rejects_string(self):
        """类型不接受字符串，必须传 ModelEventType 枚举。"""
        with pytest.raises(TypeError):
            ModelEvent(type="text", data="hello")

    def test_cannot_reassign_type(self):
        e = ModelEvent(type=ModelEventType.TEXT, data="hello")
        with pytest.raises(Exception):
            e.type = ModelEventType.DONE

    def test_cannot_reassign_data(self):
        e = ModelEvent(type=ModelEventType.TEXT, data="hello")
        with pytest.raises(Exception):
            e.data = "world"

    def test_data_dict_is_readonly(self):
        e = ModelEvent(type=ModelEventType.USAGE, data={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        assert isinstance(e.data, MappingProxyType)
        with pytest.raises(TypeError):
            e.data["prompt_tokens"] = 999

    def test_data_nested_dict_is_readonly(self):
        """深度冻结：嵌套 dict 也必须是 MappingProxyType。"""
        e = ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={"index": 0, "nested": {"a": {"b": 1}}})
        assert isinstance(e.data, MappingProxyType)
        assert isinstance(e.data["nested"], MappingProxyType)
        assert isinstance(e.data["nested"]["a"], MappingProxyType)
        with pytest.raises(TypeError):
            e.data["nested"]["a"]["b"] = 999

    def test_data_list_frozen_to_tuple(self):
        """深度冻结：list 应转为 tuple。"""
        e = ModelEvent(type=ModelEventType.TEXT, data=["a", "b"])
        assert isinstance(e.data, tuple)

    def test_data_nested_list_frozen(self):
        """深度冻结：dict 内的 list 也应转为 tuple。"""
        e = ModelEvent(type=ModelEventType.TEXT, data={"items": [1, 2, 3]})
        assert isinstance(e.data["items"], tuple)

    def test_meta_is_readonly(self):
        e = ModelEvent(type=ModelEventType.TEXT, data="hello", meta={"key": "val"})
        assert isinstance(e.meta, MappingProxyType)
        with pytest.raises(TypeError):
            e.meta["key"] = "changed"

    def test_meta_nested_frozen(self):
        """meta 深度冻结。"""
        e = ModelEvent(type=ModelEventType.TEXT, data="hello", meta={"outer": {"inner": 1}})
        assert isinstance(e.meta["outer"], MappingProxyType)

    def test_default_meta_empty(self):
        e = ModelEvent(type=ModelEventType.TEXT, data="hello")
        assert isinstance(e.meta, MappingProxyType)
        assert len(e.meta) == 0

    def test_str_data_unchanged(self):
        e = ModelEvent(type=ModelEventType.TEXT, data="hello")
        assert e.data == "hello"

    def test_int_data_unchanged(self):
        e = ModelEvent(type=ModelEventType.TEXT, data=42)
        assert e.data == 42

    def test_none_data_unchanged(self):
        e = ModelEvent(type=ModelEventType.DONE, data=None)
        assert e.data is None

    def test_tool_call_delta_data_is_readonly(self):
        e = ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
            "index": 0, "id": "call_1", "name": "f", "arguments": '{"x":1}',
        })
        assert isinstance(e.data, MappingProxyType)
        assert e.data["index"] == 0
        assert e.data["name"] == "f"
        with pytest.raises(TypeError):
            e.data["arguments"] = "modified"

    def test_all_enum_members(self):
        """验证所有枚举成员存在且唯一。"""
        members = {m.name for m in ModelEventType}
        assert members == {"TEXT", "REASONING", "TOOL_CALL_DELTA", "USAGE", "DONE", "ERROR"}

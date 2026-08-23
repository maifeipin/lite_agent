"""模型调用抽象层 — 封装 OpenAI / Gemini 协议差异。

Agent (流式) 和 WorkerAgent (同步) 统一通过 ModelInvoker 调用 LLM，
不再直接操作 self.client.chat.completions.create 或 Gemini SDK。
"""

from abc import ABC, abstractmethod
from typing import Iterator, Optional
import time
from core.model_event import ModelEvent, ModelEventType


class ModelInvoker(ABC):
    """模型调用抽象基类。"""

    def __init__(self, model_name: str, temperature: float = 0.3,
                 max_tokens: int = 2048, output_recovery: Optional[dict] = None):
        self.model_name = model_name
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.output_recovery = dict(output_recovery or {})

    @staticmethod
    def normalize_finish_reason(value) -> str:
        reason = getattr(value, "name", value) or "stop"
        normalized = str(reason).lower()
        if normalized in {"length", "max_tokens", "max_output_tokens"}:
            return "length"
        return normalized

    def output_limit_retry_kwargs(self) -> dict:
        return dict(self.output_recovery.get("retry_kwargs") or {})

    @abstractmethod
    def invoke_stream(self, messages: list, tools: Optional[list] = None,
                      **kwargs) -> Iterator[ModelEvent]:
        """流式调用，逐个产出 ModelEvent。"""
        ...

    @abstractmethod
    def invoke_sync(self, messages: list, tools: Optional[list] = None,
                    **kwargs) -> dict:
        """同步调用，返回统一结构 dict:
        {content, tool_calls, finish_reason, usage_total, empty}
        """
        ...


class OpenAIInvoker(ModelInvoker):
    """OpenAI 兼容协议调用器（流式 + 同步）。"""

    def __init__(self, client, model_name: str, temperature: float = 0.3,
                 max_tokens: int = 2048, timeout: float = 60.0,
                 output_recovery: Optional[dict] = None):
        super().__init__(model_name, temperature, max_tokens, output_recovery)
        self.client = client
        self.timeout = timeout

    # ---- 流式调用 ----

    def invoke_stream(self, messages: list, tools: Optional[list] = None,
                      **kwargs) -> Iterator[ModelEvent]:
        """流式调用 OpenAI API，逐 chunk 产出 ModelEvent。

        产出顺序: text/reasoning/tool_call_delta 交错 → usage → done
        若发生异常，产出 error 事件后停止。
        """
        call_kwargs = self._build_call_kwargs(messages, tools, stream=True, **kwargs)
        _finish_reason = "stop"
        try:
            with self.client.chat.completions.create(**call_kwargs) as response_stream:
                for chunk in response_stream:
                    if getattr(chunk, "usage", None):
                        yield ModelEvent(type=ModelEventType.USAGE, data={
                            "prompt_tokens": chunk.usage.prompt_tokens or 0,
                            "completion_tokens": chunk.usage.completion_tokens or 0,
                            "total_tokens": chunk.usage.total_tokens or 0,
                        })

                    if not chunk.choices:
                        continue
                    choice0 = chunk.choices[0]
                    if getattr(choice0, "finish_reason", None):
                        _finish_reason = self.normalize_finish_reason(choice0.finish_reason)
                    delta = choice0.delta
                    if not delta:
                        continue

                    if getattr(delta, "content", None):
                        yield ModelEvent(type=ModelEventType.TEXT, data=delta.content)

                    if getattr(delta, "reasoning_content", None):
                        yield ModelEvent(type=ModelEventType.REASONING, data=delta.reasoning_content)

                    if getattr(delta, "tool_calls", None):
                        for tc_delta in delta.tool_calls:
                            yield ModelEvent(type=ModelEventType.TOOL_CALL_DELTA, data={
                                "index": tc_delta.index if tc_delta.index is not None else 0,
                                "id": getattr(tc_delta, "id", None),
                                "name": getattr(tc_delta.function, "name", None) if getattr(tc_delta, "function", None) else None,
                                "arguments": getattr(tc_delta.function, "arguments", None) if getattr(tc_delta, "function", None) else None,
                            })

                yield ModelEvent(type=ModelEventType.DONE, data={"finish_reason": _finish_reason})

        except Exception as e:
            yield ModelEvent(type=ModelEventType.ERROR, data=e)

    # ---- 同步调用 ----

    def invoke_sync(self, messages: list, tools: Optional[list] = None,
                    **kwargs) -> dict:
        call_kwargs = self._build_call_kwargs(messages, tools, stream=False, **kwargs)
        start_t = time.time()
        client = self.client
        if "max_retries" in kwargs and hasattr(client, "with_options"):
            client = client.with_options(max_retries=int(kwargs["max_retries"]))
        response = client.chat.completions.create(**call_kwargs)

        choice = response.choices[0]
        tool_calls = []
        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in choice.message.tool_calls
            ]

        return {
            "content": choice.message.content or "",
            "tool_calls": tool_calls,
            "finish_reason": self.normalize_finish_reason(choice.finish_reason),
            "usage_total": response.usage.total_tokens if response.usage else 0,
            "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) if response.usage else 0,
            "completion_tokens": getattr(response.usage, "completion_tokens", 0) if response.usage else 0,
            "empty": False,
        }

    # ---- 内部方法 ----

    def _build_call_kwargs(self, messages: list, tools: Optional[list] = None,
                           stream: bool = False, **kwargs) -> dict:
        """组装 OpenAI API 调用参数。"""
        call_kwargs = {
            "model": self.model_name,
            "messages": messages,
            "stream": stream,
        }
        if stream:
            call_kwargs["stream_options"] = {"include_usage": True}

        has_requested_thinking = "thinking" in kwargs
        requested_thinking = kwargs.get("thinking")
        requested_extra_body = kwargs.get("extra_body") or {}
        has_explicit_recovery = has_requested_thinking or bool(requested_extra_body)
        is_pro = "pro" in self.model_name.lower() or "reasoner" in self.model_name.lower()
        if has_explicit_recovery:
            call_kwargs["extra_body"] = dict(requested_extra_body)
            if has_requested_thinking:
                call_kwargs["extra_body"]["thinking"] = requested_thinking
        elif is_pro:
            call_kwargs["reasoning_effort"] = "high"
            call_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        if not is_pro or has_explicit_recovery:
            call_kwargs["temperature"] = self.temperature
            call_kwargs["max_tokens"] = kwargs.get("max_tokens", self.max_tokens)

        if tools:
            call_kwargs["tools"] = tools
            call_kwargs["tool_choice"] = "auto"

        # Kept provider-neutral at the Gateway boundary; OpenAI-compatible
        # endpoints use this to enforce structured committee/reviewer output.
        if kwargs.get("response_format"):
            call_kwargs["response_format"] = kwargs["response_format"]

        call_kwargs["timeout"] = kwargs.get("timeout", self.timeout)
        return call_kwargs


class GeminiInvoker(ModelInvoker):
    """Gemini 协议调用器（仅同步）。

    tools 参数语义与 OpenAIInvoker 一致：
    - None 或 []：本轮不发送工具；
    - 非空列表：只发送这些 OpenAI 格式的 Schema，Invoker 内部做格式转换。
    """

    def __init__(self, client, model_name: str, temperature: float = 0.3,
                 max_tokens: int = 2048, output_recovery: Optional[dict] = None):
        super().__init__(model_name, temperature, max_tokens, output_recovery)
        self.client = client

    def invoke_stream(self, messages: list, tools: Optional[list] = None,
                      **kwargs) -> Iterator[ModelEvent]:
        """Gemini 暂不支持流式，直接 yield error。"""
        yield ModelEvent(type=ModelEventType.ERROR, data=NotImplementedError("Gemini streaming not supported"))

    def invoke_sync(self, messages: list, tools: Optional[list] = None,
                    **kwargs) -> dict:
        from core.gemini_codec import (
            openai_messages_to_gemini,
            gemini_response_to_unified,
            openai_tools_to_gemini_declarations,
        )
        from google.genai import types

        system_instruction, contents = openai_messages_to_gemini(messages)

        fn_decls = openai_tools_to_gemini_declarations(tools) if tools else None
        tool_config = types.Tool(function_declarations=fn_decls) if fn_decls else None

        generate_config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=self.temperature,
            max_output_tokens=kwargs.get("max_tokens", self.max_tokens),
            tools=[tool_config] if tool_config else None,
            response_mime_type=(
                "application/json" if kwargs.get("response_format") else None
            ),
            thinking_config=self._thinking_config(types, kwargs),
        )

        retry_count = max(0, int(kwargs.get("max_retries", 2)))
        max_attempts = retry_count + 1
        response = None
        for attempt in range(max_attempts):
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=generate_config,
                )
                break
            except Exception as e:
                if "429" in str(e) and attempt < max_attempts - 1:
                    print(f"  ⚠️ [Rate Limit] hit 429, sleeping 35s...")
                    time.sleep(35)
                else:
                    raise

        return gemini_response_to_unified(response)

    @staticmethod
    def _thinking_config(types, kwargs):
        if "thinking_budget" in kwargs:
            return types.ThinkingConfig(thinking_budget=kwargs["thinking_budget"])
        if kwargs.get("thinking_level"):
            return types.ThinkingConfig(thinking_level=kwargs["thinking_level"])
        return None

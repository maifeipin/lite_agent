"""AgentRuntime - 事件驱动的执行引擎。

Runtime 只持有 Invoker、SkillEngine 和本次执行状态。
不持有 Session、Memory、Channel 或 DAG。

职责边界：
  - 消费 ModelEvent 流或同步响应，产出 RuntimeEvent 流
  - 管理 messages 列表和 tool_calls 累积器
  - 通过 ExecutionContext 调用 skill_engine.execute_with_context 执行工具
  - 死循环检测、max_steps 熔断、token 预算熔断
  - 模型调用前过滤 tools，确保模型只能看到有权调用的工具

不负责：
  - 会话管理、历史持久化 (Session/Memory)
  - 用户交互、SSE 推送 (Channel)
  - 子任务编排、DAG 调度 (Orchestrator)
  - Token 记账、日额度拦截 (Session)
"""

import uuid
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from types import MappingProxyType
from typing import Any, Iterator, Optional

from core.execution import ExecutionContext, ExecutionResult, ExecutionSource, ActorType
from core.loop_detector import LoopDetector
from core.model_event import ModelEvent, ModelEventType
from core.model_invoker import ModelInvoker
from core.skill_engine import SkillEngine
from core.utils.masker import mask_secrets


class RuntimeEventType(Enum):
    """Runtime 事件类型枚举。"""
    STEP_START = auto()       # 一步模型调用开始 {step, max_steps}
    TEXT = auto()             # 文本 delta
    REASONING = auto()        # 推理 delta
    TOOL_CALLS_READY = auto() # 所有 tool_calls 已解析，即将执行 {content, tool_calls, reasoning_content}
    TOOL_CALL = auto()        # 工具调用已解析 {id, name, arguments}
    TOOL_RESULT = auto()      # 工具执行完成 {id, name, ok, output, display}
    USAGE = auto()            # token 用量快照
    STEP_END = auto()         # 一步模型调用结束 {step, usage, finish_reason}
    DONE = auto()             # 整个循环正常结束 {content, usage_total, empty, finish_reason}
    ERROR = auto()            # 错误终止 {msg}
    DEAD_LOOP = auto()        # 死循环熔断 {tool_name}
    MAX_STEPS = auto()        # 超出最大步数 {max_steps}
    TOKEN_BUDGET_EXCEEDED = auto()  # Token 预算耗尽 {budget, used}


@dataclass(frozen=True)
class RuntimeEvent:
    """Runtime 事件，深度不可变。"""
    type: RuntimeEventType
    data: Any = None
    meta: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self):
        if not isinstance(self.type, RuntimeEventType):
            raise TypeError(
                f"RuntimeEvent.type 必须是 RuntimeEventType 枚举，"
                f"收到 {self.type!r}"
            )
        from core.execution import _deep_freeze
        object.__setattr__(self, 'data', _deep_freeze(self.data))
        if not isinstance(self.meta, MappingProxyType):
            object.__setattr__(self, 'meta', _deep_freeze(self.meta) if isinstance(self.meta, dict) else MappingProxyType({}))


@dataclass
class RuntimeState:
    """单次 run() 期间的 mutable 状态，不跨会话。"""
    messages: list = field(default_factory=list)
    step: int = 0
    total_tokens: int = 0
    text_content: str = ""
    reasoning_content: str = ""
    finish_reason: str = "stop"
    empty: bool = False  # 模型返回空响应（如 Gemini 安全过滤/无候选）
    tool_calls: list = field(default_factory=list)  # [{id, name, arguments}]


class AgentRuntime:
    """事件驱动的 Agent 执行引擎。

    持有：
      - model_invoker: ModelInvoker 实例（OpenAI/Gemini）
      - skill_engine: SkillEngine 实例

    不持有：
      - Session/Memory/Channel/DAG
      - LoopDetector（每次 run() 内创建，不跨执行共享）
    """

    def __init__(self, model_invoker: ModelInvoker, skill_engine: SkillEngine,
                 max_steps: int = 8, max_tokens: int = 2048,
                 max_retries: int = 1, non_retryable_exceptions: tuple = ()):
        self.model_invoker = model_invoker
        self.skill_engine = skill_engine
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.non_retryable_exceptions = non_retryable_exceptions

    def run(self, messages: list, tools: list,
            ctx: ExecutionContext,
            timeout: float = 60.0,
            stream: bool = True) -> Iterator[RuntimeEvent]:
        """事件驱动的执行循环。

        参数：
          messages: 初始消息列表（system + user），Runtime 会追加 assistant/tool 消息
          tools: OpenAI 格式的工具 Schema 列表（调用方已解析权限）
          ctx: 执行上下文（权限、来源、预算）
          timeout: 模型调用超时
          stream: True=流式（Agent），False=同步（Worker/Gemini）

        产出 RuntimeEvent 序列，调用方负责消费和持久化。
        """
        # 每次 run() 创建独立 LoopDetector，不跨执行共享
        loop_detector = LoopDetector()

        # 预算取 Runtime 配置与 Context 的较小值
        effective_max_steps = min(self.max_steps, ctx.max_steps)
        effective_max_tokens = min(self.max_tokens, ctx.max_output_tokens)
        token_budget = ctx.token_budget

        state = RuntimeState(messages=list(messages))
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # 过滤 tools：只把 ctx.allowed_tools 允许的工具传给模型
        effective_tools = self._filter_tools(tools, ctx)

        for step in range(effective_max_steps):
            state.step = step

            # Token 预算前置检查
            if token_budget is not None and total_usage["total_tokens"] >= token_budget:
                yield RuntimeEvent(
                    type=RuntimeEventType.TOKEN_BUDGET_EXCEEDED,
                    data={"budget": token_budget, "used": total_usage["total_tokens"]},
                )
                yield RuntimeEvent(type=RuntimeEventType.DONE,
                                   data={"content": "⚠️ Token 预算已耗尽。",
                                         "usage_total": total_usage["total_tokens"]})
                return

            yield RuntimeEvent(
                type=RuntimeEventType.STEP_START,
                data={"step": step + 1, "max_steps": effective_max_steps},
            )

            # ---- 调用模型 ----
            step_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            state.text_content = ""
            state.reasoning_content = ""
            state.finish_reason = "stop"
            state.empty = False
            state.tool_calls = []
            tool_calls_acc = {}  # {index: {id, name, arguments, provider_metadata}}

            try:
                if stream:
                    yield from self._consume_stream(
                        state, effective_tools, step_usage, tool_calls_acc, timeout,
                        effective_max_tokens,
                    )
                else:
                    yield from self._consume_sync(
                        state, effective_tools, step_usage, tool_calls_acc,
                        effective_max_tokens,
                    )
            except Exception as e:
                yield RuntimeEvent(type=RuntimeEventType.ERROR,
                                   data={"msg": f"模型调用失败: {e}"})
                yield RuntimeEvent(type=RuntimeEventType.DONE,
                                   data={"content": "", "usage_total": total_usage["total_tokens"]})
                return

            # 记账
            total_usage["prompt_tokens"] += step_usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += step_usage.get("completion_tokens", 0)
            total_usage["total_tokens"] += step_usage.get("total_tokens", 0)

            yield RuntimeEvent(
                type=RuntimeEventType.STEP_END,
                data={"step": step + 1, "usage": step_usage, "finish_reason": state.finish_reason},
            )

            # Token 预算后置检查：本步消耗后超限 -> 熔断，不执行有副作用的工具
            if token_budget is not None and total_usage["total_tokens"] >= token_budget:
                yield RuntimeEvent(
                    type=RuntimeEventType.TOKEN_BUDGET_EXCEEDED,
                    data={"budget": token_budget, "used": total_usage["total_tokens"]},
                )
                content = state.text_content.strip() or "⚠️ Token 预算已耗尽。"
                state.messages.append({"role": "assistant", "content": content})
                yield RuntimeEvent(type=RuntimeEventType.DONE,
                                   data={"content": content,
                                         "usage_total": total_usage["total_tokens"]})
                return

            # ---- 无工具调用 -> 最终回复 ----
            if not tool_calls_acc:
                content = state.text_content.strip()
                # empty 仅反映 invoker 的显式标记（如 Gemini 安全过滤/无候选），
                # 不与 content 是否为空合并；普通空响应由调用方根据 content 自行判定
                # 先持久化到 state.messages，再 yield DONE，避免消费者提前 return 导致终态丢失
                state.messages.append({"role": "assistant", "content": content})
                yield RuntimeEvent(type=RuntimeEventType.DONE,
                                   data={"content": content,
                                         "usage_total": total_usage["total_tokens"],
                                         "finish_reason": state.finish_reason,
                                         "empty": state.empty})
                return

            # ---- 有工具调用 ----
            # 补 id（provider 不发时）
            for acc in tool_calls_acc.values():
                if not acc["id"]:
                    acc["id"] = f"call_{uuid.uuid4().hex[:8]}"

            # 排序后构造 tool_calls
            sorted_calls = [tool_calls_acc[idx] for idx in sorted(tool_calls_acc)]
            state.tool_calls = sorted_calls

            tool_calls_data = []
            for tc in sorted_calls:
                item = {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    },
                }
                # 保留 provider 特有字段（Gemini thought_signature / call_id 等）
                provider_metadata = tc.get("provider_metadata") or {}
                if provider_metadata:
                    item["provider_metadata"] = provider_metadata
                tool_calls_data.append(item)
            state.messages.append({
                "role": "assistant",
                "content": state.text_content or "",
                "tool_calls": tool_calls_data,
            })

            # 发射 TOOL_CALLS_READY：让调用方在工具执行前持久化完整 assistant tool_calls
            # (含 provider_metadata)，满足 OpenAI 协议对 tool_call_id 关联的强约束
            yield RuntimeEvent(
                type=RuntimeEventType.TOOL_CALLS_READY,
                data={
                    "content": state.text_content or "",
                    "tool_calls": tool_calls_data,
                    "reasoning_content": state.reasoning_content or "",
                },
            )

            # 逐个执行工具
            for tc in sorted_calls:
                yield RuntimeEvent(
                    type=RuntimeEventType.TOOL_CALL,
                    data={"id": tc["id"], "name": tc["name"],
                          "arguments": tc["arguments"],
                          "step": state.step},
                )

                # 死循环检测
                if loop_detector.check(tc["name"], tc["arguments"]):
                    warning = loop_detector.warning(tc["name"])
                    yield RuntimeEvent(type=RuntimeEventType.DEAD_LOOP,
                                       data={"tool_name": tc["name"], "msg": warning})
                    yield RuntimeEvent(type=RuntimeEventType.DONE,
                                       data={"content": warning, "usage_total": total_usage["total_tokens"]})
                    return

                # 执行工具 - 始终通过 execute_with_context，不做权限预判
                # 使用 monotonic clock 测量执行耗时，供账本索引投影
                _tool_t0 = time.monotonic()
                ok, output = self._execute_tool(tc, ctx)
                tool_duration_ms = int((time.monotonic() - _tool_t0) * 1000)

                state.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["name"],
                    "content": output,
                })

                # 构造 display：脱敏 + 截断
                display = output if len(output) <= 1000 else output[:1000] + "...(截断)"
                display = mask_secrets(display) if display else display

                yield RuntimeEvent(
                    type=RuntimeEventType.TOOL_RESULT,
                    data={
                        "id": tc["id"],
                        "name": tc["name"],
                        "arguments": tc["arguments"],  # 工具调用参数
                        "ok": ok,
                        "output": output,       # 完整结果，供持久化
                        "display": display,     # 脱敏截断，供 UI 展示
                        "step": state.step,
                        "duration_ms": tool_duration_ms,
                    },
                )

            # 工具结果已回传，进入下一轮

        # 超出最大步数
        warning = "⚠️ 任务执行步骤过多，已自动终止。请尝试拆分为更小的任务。"
        state.messages.append({"role": "assistant", "content": warning})
        yield RuntimeEvent(type=RuntimeEventType.MAX_STEPS, data={"max_steps": effective_max_steps})
        yield RuntimeEvent(type=RuntimeEventType.DONE,
                           data={"content": warning, "usage_total": total_usage["total_tokens"]})

    # ------------------------------------------------------------------
    #  模型调用消费
    # ------------------------------------------------------------------

    def _consume_stream(self, state: RuntimeState, tools: list,
                        step_usage: dict, tool_calls_acc: dict,
                        timeout: float, max_tokens: int) -> Iterator[RuntimeEvent]:
        """流式消费 ModelEvent 流，支持首字前重试。

        重试语义：
          - 若已向调用方产出 TEXT/REASONING 事件，或已累积 TOOL_CALL_DELTA，则不可重试
          - 若未产出任何内容且异常非不可重试类型，最多重试 max_retries 次
          - USAGE 事件延迟到流成功结束后才发射，避免重试时重复计费
        """
        last_exception: Exception = None
        for attempt in range(self.max_retries + 1):
            # 本次尝试的局部累积器：仅在成功后合并到全局累积器
            attempt_tool_calls: dict = {}
            attempt_usage: dict = {}
            has_output = False  # 是否已产出 TEXT/REASONING/TOOL_CALL_DELTA

            try:
                for event in self.model_invoker.invoke_stream(
                    state.messages, tools, timeout=timeout, max_tokens=max_tokens,
                ):
                    if event.type == ModelEventType.TEXT:
                        has_output = True
                        state.text_content += event.data
                        yield RuntimeEvent(type=RuntimeEventType.TEXT, data=event.data)
                    elif event.type == ModelEventType.REASONING:
                        has_output = True
                        state.reasoning_content += event.data
                        yield RuntimeEvent(type=RuntimeEventType.REASONING, data=event.data)
                    elif event.type == ModelEventType.TOOL_CALL_DELTA:
                        d = event.data
                        idx = d["index"]
                        acc = attempt_tool_calls.setdefault(
                            idx, {"id": "", "name": "", "arguments": "", "provider_metadata": {}}
                        )
                        if d.get("id"):
                            acc["id"] = d["id"]
                        if d.get("name"):
                            acc["name"] = d["name"]
                        if d.get("arguments"):
                            acc["arguments"] += d["arguments"]
                        # 保留 provider 特有字段（如 Gemini thought_signature）
                        pm = d.get("provider_metadata")
                        if pm:
                            acc["provider_metadata"].update(pm)
                        has_output = True
                    elif event.type == ModelEventType.USAGE:
                        attempt_usage.update(dict(event.data))
                    elif event.type == ModelEventType.DONE:
                        state.finish_reason = event.data.get("finish_reason", "stop")
                    elif event.type == ModelEventType.ERROR:
                        raise event.data

                # 成功消费完流，合并本次累积器到全局
                tool_calls_acc.update(attempt_tool_calls)
                step_usage.update(attempt_usage)
                # 延迟发射 USAGE，避免重试时重复计费
                if attempt_usage:
                    yield RuntimeEvent(type=RuntimeEventType.USAGE, data=dict(step_usage))
                return
            except Exception as e:
                last_exception = e
                # 已向调用方产出内容 -> 不可重试（流已部分发送）
                if has_output:
                    raise
                # 不可重试的异常类型（4xx 鉴权/限流等）
                if isinstance(e, self.non_retryable_exceptions):
                    raise
                # 已用完重试次数
                if attempt >= self.max_retries:
                    raise
                # 可以重试：重置 finish_reason，进入下一次尝试
                state.finish_reason = "stop"
                continue
        # 不应到达此处
        if last_exception:
            raise last_exception

    def _consume_sync(self, state: RuntimeState, tools: list,
                      step_usage: dict, tool_calls_acc: dict,
                      max_tokens: int) -> Iterator[RuntimeEvent]:
        """同步消费 invoke_sync 返回的 dict，转换为 RuntimeEvent。"""
        result = self.model_invoker.invoke_sync(state.messages, tools, max_tokens=max_tokens)

        content = result.get("content", "")
        if content:
            state.text_content = content
            yield RuntimeEvent(type=RuntimeEventType.TEXT, data=content)

        for tc in result.get("tool_calls", []):
            idx = len(tool_calls_acc)
            tool_calls_acc[idx] = {
                "id": tc["id"],
                "name": tc["name"],
                "arguments": tc["arguments"],
                # 保留 provider 特有字段（Gemini thought_signature 等）
                "provider_metadata": dict(tc.get("provider_metadata") or {}),
            }

        usage_total = result.get("usage_total", 0)
        if usage_total:
            step_usage["total_tokens"] = usage_total
            yield RuntimeEvent(type=RuntimeEventType.USAGE, data=dict(step_usage))

        state.finish_reason = result.get("finish_reason", "stop")
        state.empty = bool(result.get("empty", False))

    # ------------------------------------------------------------------
    #  工具过滤与执行
    # ------------------------------------------------------------------

    def _filter_tools(self, tools: list, ctx: ExecutionContext) -> list:
        """过滤 tools：只保留 ctx.allowed_tools 允许的工具。

        ctx.allowed_tools=None -> 不过滤（允许全部）
        ctx.allowed_tools=[]   -> 返回空列表（禁止全部）
        ctx.allowed_tools=[...] -> 只保留指定工具
        """
        if ctx.allowed_tools is None:
            return tools
        return [t for t in tools if t.get("function", {}).get("name") in ctx.allowed_tools]

    def _execute_tool(self, tc: dict, ctx: ExecutionContext) -> tuple[bool, str]:
        """执行单个工具调用，返回 (ok, output)。

        始终通过 execute_with_context 执行，权限检查和审计由执行网关统一处理。
        """
        try:
            result = self.skill_engine.execute_with_context(ctx, tc["name"], tc["arguments"])
            if isinstance(result, ExecutionResult):
                return result.ok, result.output
            # Legacy 路径返回 str
            return True, str(result)
        except Exception as e:
            return False, f"❌ 工具执行异常: {e}"

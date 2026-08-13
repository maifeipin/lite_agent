import json
import time
import traceback
import collections
from typing import Callable
from core.skill_engine import SkillEngine
from core.subtask_dag import Subtask
from core.model_config import is_gemini_driver, supports_vision


class LRUCache:
    def __init__(self, maxsize=200):
        self.cache = collections.OrderedDict()
        self.maxsize = maxsize

    def setdefault(self, key, default):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        self.cache[key] = default
        if len(self.cache) > self.maxsize:
            self.cache.popitem(last=False)
        return self.cache[key]

    def get(self, key, default=None):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return default

    def __getitem__(self, key):
        self.cache.move_to_end(key)
        return self.cache[key]

    def __setitem__(self, key, value):
        self.cache[key] = value
        self.cache.move_to_end(key)
        if len(self.cache) > self.maxsize:
            self.cache.popitem(last=False)


class WorkerAgent:

    def __init__(self, name: str, client, model_name: str,
                 model_cfg: dict, skill_engine: SkillEngine,
                 tools_allowlist: list = None, driver: str = "openai",
                 log_callback: Callable = None):
        self.name = name
        self.client = client
        self.model_name = model_name
        self.model_cfg = model_cfg
        self.skill_engine = skill_engine
        self.tools_allowlist = tools_allowlist
        self.driver = driver
        self.log_callback = log_callback
        self.max_steps = model_cfg.get("max_steps", 8)
        self.max_tokens = model_cfg.get("max_tokens", 2048)
        self.temperature = model_cfg.get("temperature", 0.3)
        self._dead_loop_counter = LRUCache(maxsize=200)

    def _log(self, msg: str):
        print(msg)
        if self.log_callback:
            try:
                self.log_callback(str(msg))
            except Exception:
                pass

    def _get_tools(self):
        all_tools = self.skill_engine.get_all_schemas()
        if not self.tools_allowlist:
            return all_tools
        allowlist = set(self.tools_allowlist)
        return [t for t in all_tools if t["function"]["name"] in allowlist]

    def _build_prompt(self, subtask: Subtask, upstream: dict = None,
                      goal: str = None, global_strategy: str = None) -> str:
        tools_desc = self.skill_engine.list_skills_filtered(self.tools_allowlist)
        ctx_block = ""
        if upstream:
            ctx_lines = []
            for dep_id, dep_result in upstream.items():
                if isinstance(dep_result, dict):
                    res_text = dep_result.get("result", "")
                    tool_res = dep_result.get("tool_results", [])

                    block = f"### {dep_id}\n[执行结论]:\n{res_text[:8000]}\n"
                    if tool_res:
                        block += "\n[工具调用明细]:\n"
                        for tr in tool_res:
                            block += f"- 工具 `{tr.get('name')}(args={tr.get('args')})`:\n返回数据: {str(tr.get('result', ''))}\n"
                    ctx_lines.append(block)
                else:
                    # Legacy fallback for old string formats
                    ctx_lines.append(f"### {dep_id}\n{str(dep_result)[:1500]}")

            ctx_block = "\n\n上游子任务结果（参考上下文）:\n" + "\n\n".join(ctx_lines)

            # Total fan-in truncation to prevent context explosion (~16K+ tokens if CJK)
            if len(ctx_block) > 24000:
                ctx_block = ctx_block[:24000] + "\n\n... ⚠️ [上游已截断, 依赖的部分内容被省略] ..."

        goal_block = ""
        if goal:
            goal_block = f"## 总体目标 (北极星目标)\n{goal}\n"

        strategy_block = ""
        if global_strategy:
            strategy_block = (
                f"## 全局战略 (由 Planner 制定，本 DAG 所有 Worker 共享)\n"
                f"{global_strategy}\n"
                f"⚠️ 严格在以上战略框架内执行当前子任务，不要偏离或自行扩大范围。\n"
            )

        return f"""你是 {self.name}，专门处理 {subtask.type.value} 类任务。

{goal_block}{strategy_block}
## 当前子任务
{subtask.name}: {subtask.prompt}
{ctx_block}

可用工具:
{tools_desc}

规则:
- 严格在全局战略框架内执行，不要偏离
- 你的输出将被下游子任务消费，请确保结果完整可用
- 如果某工具连续失败 2 次，改用备选方案，不要死磕
- 需要工具时直接调用，返回结果后继续推理
- 完成后给出清晰的结果总结
- 不要编造数据，以工具返回的真实结果为准"""

    def run(self, subtask: Subtask, upstream: dict = None,
            images: list = None, goal: str = None,
            global_strategy: str = None) -> tuple[str, list]:
        system_msg = {
            "role": "system",
            "content": self._build_prompt(subtask, upstream, goal, global_strategy),
        }
        messages = [system_msg]

        if images and self._supports_vision():
            user_content = [{"type": "text", "text": subtask.prompt}]
            for img_url in images:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": img_url, "detail": "auto"},
                })
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": subtask.prompt})

        tools = self._get_tools()
        extracted_tools = []

        for step in range(self.max_steps):
            try:
                subtask.steps_used += 1
                result = self._call_model(messages, tools)
            except Exception as e:
                traceback.print_exc()
                return f"❌ LLM 调用失败: {e}", extracted_tools

            if result.get("usage_total"):
                subtask.token_usage += result["usage_total"]

            tool_calls = result.get("tool_calls") or []
            if tool_calls:
                tool_calls_data = []
                for tc in tool_calls:
                    item = {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    if tc.get("provider_metadata"):
                        item["provider_metadata"] = tc["provider_metadata"]
                    tool_calls_data.append(item)
                messages.append({
                    "role": "assistant",
                    "content": result.get("content") or "",
                    "tool_calls": tool_calls_data,
                })

                for tc in tool_calls:
                    if self._check_dead_loop(
                        tc["name"], tc["arguments"], messages
                    ):
                        return self._dead_loop_msg(tc["name"]), extracted_tools

                    self._log(
                        f"  🔧 [{self.name}] [{step + 1}/{self.max_steps}] "
                        f"{tc['name']}({tc['arguments'][:80]})"
                    )
                    tool_result = self.skill_engine.execute(
                        tc["name"], tc["arguments"]
                    )
                    extracted_tools.append(self._extract_tool_result(
                        tc["name"], tc["arguments"], tool_result
                    ))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["name"],
                        "content": tool_result,
                    })
                continue

            reply = result.get("content") or ""
            if not reply:
                reply = "(空回复 - 安全过滤)" if result.get("empty") else "(空回复)"
            messages.append({"role": "assistant", "content": reply})
            return reply, extracted_tools

        return "⚠️ 子任务执行步骤过多，已自动终止", extracted_tools

    # ------------------------------------------------------------------
    #  统一调用层：协议差异只体现在这里，上层 Tool Loop 不再分支。
    # ------------------------------------------------------------------
    def _call_model(self, messages: list, tools: list) -> dict:
        """发起一次模型请求并返回统一结构 dict。"""
        if is_gemini_driver(self.driver):
            return self._call_gemini(messages, tools)
        return self._call_openai(messages, tools)

    def _call_openai(self, messages: list, tools: list) -> dict:
        actual_model = self.model_cfg.get("model", self.model_name)
        kwargs = {"model": actual_model, "messages": messages}

        if (
            "pro" in self.model_name.lower()
            or "reasoner" in self.model_name.lower()
        ):
            kwargs["reasoning_effort"] = "high"
        else:
            kwargs["temperature"] = self.temperature
            kwargs["max_tokens"] = self.max_tokens

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        kwargs["timeout"] = 60.0

        start_t = time.time()
        self._log(f"  🧠 [LLM Request] 角色: {self.name}, 模型: {actual_model}")
        response = self.client.chat.completions.create(**kwargs)
        self._log(
            f"  ✅ [LLM Response] 耗时: {time.time() - start_t:.2f}s, "
            f"Tokens: {response.usage.total_tokens if response.usage else 0}"
        )

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
            "finish_reason": choice.finish_reason,
            "usage_total": response.usage.total_tokens if response.usage else 0,
            "empty": False,
        }

    def _call_gemini(self, messages: list, tools: list) -> dict:
        from core.gemini_codec import (
            openai_messages_to_gemini,
            gemini_response_to_unified,
        )
        from google.genai import types

        gemini_model = self.model_cfg.get("model", self.model_name)
        system_instruction, contents = openai_messages_to_gemini(messages)

        tool_names = self.tools_allowlist if self.tools_allowlist else None
        fn_decls = self.skill_engine.get_gemini_tool_declarations(tool_names)
        tool_config = types.Tool(function_declarations=fn_decls) if fn_decls else None

        generate_config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=self.temperature,
            max_output_tokens=self.max_tokens,
            tools=[tool_config] if tool_config else None,
        )

        start_t = time.time()
        self._log(f"  🧠 [LLM Request] 角色: {self.name}, 模型: {gemini_model} (gemini)")

        max_retries = 3
        response = None
        for attempt in range(max_retries):
            try:
                response = self.client.models.generate_content(
                    model=gemini_model,
                    contents=contents,
                    config=generate_config,
                )
                break
            except Exception as e:
                if "429" in str(e) and attempt < max_retries - 1:
                    print(f"  ⚠️ [Rate Limit] {self.name} hit 429, sleeping 35s...")
                    time.sleep(35)
                else:
                    raise

        self._log(f"  ✅ [LLM Response] 耗时: {time.time() - start_t:.2f}s")
        return gemini_response_to_unified(response)

    # ------------------------------------------------------------------
    #  共享工具方法
    # ------------------------------------------------------------------
    def _extract_tool_result(self, name: str, args: str, raw_result) -> dict:
        from core.skill_engine import _cap_tool_result
        res_str = str(raw_result)
        return {
            "name": name,
            "args": args,
            "result": _cap_tool_result(name, res_str, max_len=4000)
        }

    def _check_dead_loop(self, tool_name: str, args_str: str,
                         _messages=None) -> bool:
        fingerprint = f"{tool_name}:{args_str}"
        counter = self._dead_loop_counter
        if fingerprint == counter.get("_last"):
            counter["_streak"] = counter.get("_streak", 1) + 1
        else:
            counter["_streak"] = 1
        counter["_last"] = fingerprint
        return counter["_streak"] >= 3

    def _dead_loop_msg(self, tool_name: str) -> str:
        print(
            f"  🔄 [{self.name}] 死循环: {tool_name} "
            f"x{self._dead_loop_counter.get('_streak', 0)}"
        )
        return (
            f"死循环终止: {tool_name} "
            f"连续重复 {self._dead_loop_counter.get('_streak', 0)} 次"
        )

    def _supports_vision(self) -> bool:
        return supports_vision(self.model_cfg.get("tags", []))

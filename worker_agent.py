import json
import time
import traceback
import collections
from openai import OpenAI
from skill_engine import SkillEngine
from subtask_dag import Subtask

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


class WorkerAgent:

    def __init__(self, name: str, client, model_name: str,
                 model_cfg: dict, skill_engine: SkillEngine,
                 tools_allowlist: list = None, provider: str = "openai"):
        self.name = name
        self.client = client
        self.model_name = model_name
        self.model_cfg = model_cfg
        self.skill_engine = skill_engine
        self.tools_allowlist = tools_allowlist
        self.provider = provider
        self.max_steps = model_cfg.get("max_steps", 8)
        self.max_tokens = model_cfg.get("max_tokens", 2048)
        self.temperature = model_cfg.get("temperature", 0.3)
        self._dead_loop_counter = LRUCache(maxsize=200)

    def _get_tools(self):
        all_tools = self.skill_engine.get_all_schemas()
        if not self.tools_allowlist:
            return all_tools
        allowlist = set(self.tools_allowlist)
        return [t for t in all_tools if t["function"]["name"] in allowlist]

    def _build_prompt(self, subtask: Subtask, upstream: dict = None) -> str:
        tools_desc = self.skill_engine.list_skills_filtered(self.tools_allowlist)
        ctx_block = ""
        if upstream:
            ctx_lines = []
            for dep_id, dep_result in upstream.items():
                ctx_lines.append(f"### {dep_id}\n{dep_result[:1500]}")
            ctx_block = (
                "\n\n上游子任务结果（参考上下文）:\n" + "\n\n".join(ctx_lines)
            )

        return f"""你是 {self.name}，专门处理 {subtask.type.value} 类任务。

当前子任务: {subtask.name}
{ctx_block}

可用工具:
{tools_desc}

规则:
- 只处理当前子任务，不越界
- 需要工具时直接调用，返回结果后继续推理
- 完成后给出清晰的结果总结
- 不要编造数据，以工具返回的真实结果为准"""

    def run(self, subtask: Subtask, upstream: dict = None,
            images: list = None) -> str:
        if self.provider == "gemini":
            return self._run_gemini(subtask, upstream, images)
        return self._run_openai(subtask, upstream, images)

    # ==================================================================
    #  OpenAI 路径 (原有)
    # ==================================================================
    def _run_openai(self, subtask: Subtask, upstream: dict = None,
                    images: list = None) -> str:
        system_msg = {
            "role": "system",
            "content": self._build_prompt(subtask, upstream),
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

        for step in range(self.max_steps):
            try:
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
                print(f"  🧠 [LLM Request] 角色: {self.name}, 模型: {actual_model}")
                response = self.client.chat.completions.create(**kwargs)
                print(f"  ✅ [LLM Response] 耗时: {time.time()-start_t:.2f}s, Tokens: {response.usage.total_tokens if response.usage else 0}")
                
                choice = response.choices[0]

                if response.usage:
                    subtask.token_usage += response.usage.total_tokens

            except Exception as e:
                traceback.print_exc()
                return f"❌ LLM 调用失败: {e}"

            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                tool_calls_data = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in choice.message.tool_calls
                ]
                messages.append({
                    "role": "assistant",
                    "content": choice.message.content or "",
                    "tool_calls": tool_calls_data,
                })

                for tc in choice.message.tool_calls:
                    if self._check_dead_loop(
                        tc.function.name, tc.function.arguments, messages
                    ):
                        return self._dead_loop_msg(tc.function.name)

                    print(
                        f"  🔧 [{self.name}] [{step + 1}/{self.max_steps}] "
                        f"{tc.function.name}({tc.function.arguments[:80]})"
                    )
                    result = self.skill_engine.execute(
                        tc.function.name, tc.function.arguments
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": result,
                    })
                continue

            reply = choice.message.content or "(空回复)"
            messages.append({"role": "assistant", "content": reply})
            return reply

        return "⚠️ 子任务执行步骤过多，已自动终止"

    # ==================================================================
    #  Gemini 路径 (google-genai)
    # ==================================================================
    def _run_gemini(self, subtask: Subtask, upstream: dict = None,
                    images: list = None) -> str:
        from google.genai import types

        system_text = self._build_prompt(subtask, upstream)
        gemini_model = self.model_cfg.get("model", self.model_name)

        tool_names = self.tools_allowlist if self.tools_allowlist else None
        fn_decls = self.skill_engine.get_gemini_tool_declarations(tool_names)
        tool_config = types.Tool(function_declarations=fn_decls) if fn_decls else None

        generate_config = types.GenerateContentConfig(
            system_instruction=system_text,
            temperature=self.temperature,
            max_output_tokens=self.max_tokens,
            tools=[tool_config] if tool_config else None,
        )

        contents = [subtask.prompt]

        if images and self._supports_vision():
            parts = [types.Part(text=subtask.prompt)]
            for img_url in images:
                parts.append(types.Part.from_uri(
                    file_uri=img_url, mime_type="image/jpeg"
                ))
            contents = [types.Content(role="user", parts=parts)]

        for step in range(self.max_steps):
            try:
                response = self.client.models.generate_content(
                    model=gemini_model,
                    contents=contents,
                    config=generate_config,
                )
            except Exception as e:
                traceback.print_exc()
                return f"❌ Gemini 调用失败: {e}"

            if not response.candidates:
                return f"❌ Gemini 无候选回复 (可能是安全过滤或配额问题)"

            candidate = response.candidates[0]

            if response.usage_metadata:
                subtask.token_usage += response.usage_metadata.total_token_count

            if not candidate.content or not candidate.content.parts:
                finish_reason = str(candidate.finish_reason) if hasattr(candidate, 'finish_reason') else "unknown"
                if candidate.finish_reason and hasattr(candidate.finish_reason, 'name'):
                    finish_reason = candidate.finish_reason.name
                if "STOP" in str(finish_reason).upper():
                    return "(空回复 - 安全过滤)"
                return f"❌ 异常终止: finish_reason={finish_reason}"

            has_function_call = False
            text_parts = []
            function_calls = []

            for part in candidate.content.parts:
                if part.text:
                    text_parts.append(part.text)
                if hasattr(part, "function_call") and part.function_call:
                    has_function_call = True
                    function_calls.append(part.function_call)

            if has_function_call:
                fn_response_parts = []
                for fn_call in function_calls:
                    name = fn_call.name
                    args = (
                        dict(fn_call.args)
                        if fn_call.args
                        else {}
                    )
                    args_json = json.dumps(args, ensure_ascii=False)

                    if self._check_dead_loop(name, args_json, None):
                        return self._dead_loop_msg(name)

                    print(
                        f"  🔧 [{self.name}] [{step + 1}/{self.max_steps}] "
                        f"{name}({args_json[:80]})"
                    )
                    tool_result = self.skill_engine.execute(name, args_json)

                    fn_response_part = types.Part.from_function_response(
                        name=name,
                        response={"result": tool_result},
                    )
                    fn_response_parts.append(fn_response_part)

                fn_content = types.Content(
                    role="user",
                    parts=fn_response_parts,
                )
                if isinstance(contents, list):
                    contents.append(candidate.content)
                    contents.append(fn_content)
                else:
                    contents = [candidate.content, fn_content]

                continue

            reply = "\n".join(text_parts) if text_parts else "(空回复)"
            return reply

        return "⚠️ 子任务执行步骤过多，已自动终止"

    # ==================================================================
    #  共享工具方法
    # ==================================================================
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
        tags = self.model_cfg.get("tags", [])
        return "multimodal" in tags or "vision" in tags

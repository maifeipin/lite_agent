import json
import traceback
from typing import Callable, Optional
from core.agent_runtime import AgentRuntime, RuntimeEvent, RuntimeEventType
from core.execution import ExecutionContext, ExecutionSource, ActorType
from core.execution_ledger import ExecutionLedger
from core.runtime_recorder import RuntimeRecorder
from core.skill_engine import SkillEngine, _cap_tool_result
from core.subtask_dag import Subtask
from core.model_config import is_gemini_driver, supports_vision
from core.model_invoker import OpenAIInvoker, GeminiInvoker


class WorkerAgent:

    def __init__(self, name: str, client, model_name: str,
                 model_cfg: dict, skill_engine: SkillEngine,
                 tools_allowlist: list = None, driver: str = "openai",
                 log_callback: Callable = None,
                 ledger: Optional[ExecutionLedger] = None):
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

        # ExecutionLedger: 旁路执行账本 (可选, 由调用方注入)
        self.ledger = ledger

        if is_gemini_driver(driver):
            self.model_invoker = GeminiInvoker(
                client=client,
                model_name=model_name,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        else:
            self.model_invoker = OpenAIInvoker(
                client=client,
                model_name=model_name,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

    def _log(self, msg: str):
        print(msg)
        if self.log_callback:
            try:
                self.log_callback(str(msg))
            except Exception:
                pass

    def _get_tools(self):
        """获取本轮可用工具 Schema 列表。

        tools_allowlist=None -> 返回全部工具；
        tools_allowlist=[]   -> 返回空列表（禁止全部）；
        tools_allowlist=[...] -> 只返回指定工具。
        """
        if self.tools_allowlist is None:
            return self.skill_engine.get_all_schemas()
        if not self.tools_allowlist:
            return []
        allowlist = set(self.tools_allowlist)
        return [t for t in self.skill_engine.get_all_schemas()
                if t["function"]["name"] in allowlist]

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
            global_strategy: str = None,
            parent_execution_id: str = "") -> tuple[str, list]:
        """执行子任务，返回 (reply, extracted_tools)。

        parent_execution_id: 父 Orchestrator 任务的 execution_id，
                              用于在账本中建立父子关系。
        """
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

        # 构造 ExecutionContext
        # source=ORCHESTRATOR：Worker 由 DAG/Orchestrator 分派执行，非用户直接调用
        ctx = ExecutionContext(
            actor_id=self.name,
            actor_type=ActorType.WORKER,
            source=ExecutionSource.ORCHESTRATOR,
            allowed_tools=frozenset(self.tools_allowlist) if self.tools_allowlist is not None else None,
            session_key=f"worker_{self.name}",
            max_steps=self.max_steps,
            max_output_tokens=self.max_tokens,
        )

        # 启动账本记录 (旁路, 不阻断主流程)
        execution = None
        if self.ledger is not None:
            execution = self.ledger.start(
                ctx, model_name=self.model_name, provider=self.driver,
                parent_execution_id=parent_execution_id, stream_mode=False,
            )
            import dataclasses
            ctx = dataclasses.replace(ctx, execution_id=execution.id)

        # 创建 AgentRuntime（Worker 使用同步模式）
        runtime = AgentRuntime(
            model_invoker=self.model_invoker,
            skill_engine=self.skill_engine,
            max_steps=self.max_steps,
            max_tokens=self.max_tokens,
        )

        try:
            runtime_iter = runtime.run(messages, tools, ctx, stream=False)
            # 经 Recorder 包装 (若 ledger 可用)
            if execution is not None:
                recorder = RuntimeRecorder(self.ledger, execution.id)
                event_iter = recorder.wrap(runtime_iter)
            else:
                event_iter = runtime_iter

            for event in event_iter:
                if event.type == RuntimeEventType.STEP_START:
                    subtask.steps_used += 1

                elif event.type == RuntimeEventType.USAGE:
                    subtask.token_usage += event.data.get("total_tokens", 0)

                elif event.type == RuntimeEventType.TOOL_CALL:
                    self._log(
                        f"  🔧 [{self.name}] [{subtask.steps_used}/{self.max_steps}] "
                        f"{event.data['name']}({event.data['arguments'][:80]})"
                    )

                elif event.type == RuntimeEventType.TOOL_RESULT:
                    extracted_tools.append({
                        "name": event.data["name"],
                        "args": event.data.get("arguments", ""),
                        "result": _cap_tool_result(
                            event.data["name"],
                            event.data["output"],
                            max_len=4000,
                        ),
                    })

                elif event.type == RuntimeEventType.DONE:
                    # 空响应语义（保持旧 Worker 行为）：
                    #   empty=True (Gemini 安全过滤/无候选) → "(空回复 - 安全过滤)"
                    #   empty=False 且 content 为空 (普通空响应) → "(空回复)"
                    reply = event.data.get("content", "")
                    empty = event.data.get("empty", False)
                    if empty:
                        reply = "(空回复 - 安全过滤)"
                    elif not reply:
                        reply = "(空回复)"
                    return reply, extracted_tools

                elif event.type == RuntimeEventType.ERROR:
                    return f"❌ {event.data.get('msg', '执行失败')}", extracted_tools

                elif event.type == RuntimeEventType.DEAD_LOOP:
                    return event.data.get("msg", "⚠️ 死循环检测"), extracted_tools

                elif event.type == RuntimeEventType.MAX_STEPS:
                    return "⚠️ 子任务执行步骤过多，已自动终止", extracted_tools

                elif event.type == RuntimeEventType.TOKEN_BUDGET_EXCEEDED:
                    return "⚠️ Token 预算已耗尽", extracted_tools

        except Exception as e:
            traceback.print_exc()
            # 异常退出: ledger 由 recorder.wrap 标记 failed, 兜底再 finish 一次
            if execution is not None:
                self.ledger.finish(execution.id, status="failed",
                                   terminal_reason="worker_exception")
            return f"❌ 执行失败: {e}", extracted_tools

        # 正常退出但未收到 DONE (不应发生): 标记 ledger 为 succeeded 兜底
        if execution is not None:
            self.ledger.finish(execution.id, status="succeeded",
                               terminal_reason="no_terminal_event")
        return "⚠️ 子任务执行异常终止", extracted_tools

    def _supports_vision(self) -> bool:
        return supports_vision(self.model_cfg.get("tags", []))

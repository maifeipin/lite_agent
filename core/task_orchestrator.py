import json
import os
import time
import uuid
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Callable, Optional

from core.model_router import ModelRouter
from core.model_policy import ExecutionPolicy, ModelLock, ModelSelector
from core.worker_agent import WorkerAgent, WorkerOutcome
from core.skill_engine import SkillEngine
from core.subtask_dag import Subtask, SubtaskDAG, SubtaskType, SubtaskStatus
from core.execution import ExecutionContext, ActorType, ExecutionSource
from core.execution_ledger import ExecutionLedger
from core.execution_budget import ExecutionBudget
from core.llm_gateway import LLMGateway
from core.request_selector import RequestSelector

PLANNER_PROMPT = """你是一个任务编排专家。请将以下用户目标拆解为子任务列表。

用户目标: {goal}

可用的全部工具:
{tools_desc}

请输出严格的 JSON，格式如下:
{{
  "global_strategy": "本次任务的全局战略描述。请简明扼要，控制在200字以内。",
  "subtasks": [
    {{
      "id": "sub_1",
      "name": "简短名称",
      "type": "text|code|multimodal|complex_reasoning|data_analysis",
      "prompt": "发给执行者的具体指令",
      "depends_on": [],
      "tools_hint": ["工具名1", "工具名2"]
      "execution_mode": "agent|tool",
      "tool_name": "仅 execution_mode=tool 时填写的唯一工具名",
      "tool_arguments": {{"仅确定性参数": "值"}}
    }}
  ]
}}

分类规则:
- text: 通用文本处理、翻译、总结、闲聊
- code: 代码生成、调试、审查、脚本编写
- multimodal: 图片理解、OCR、文件视觉分析
- complex_reasoning: 仅用于数学证明、多步逻辑推导、创造性方案等真正需要深度推理的任务
- data_analysis: 巡检、审计、端口研判、结构化数据归类、报表生成和趋势判断；不要因为任务含“分析”就归为 complex_reasoning

编排规则:
1. 尽可能让无依赖的子任务并行，depends_on 写依赖的 id
2. 依赖深度不超过 5 层。全局预算: 所有子任务的 LLM 交互步数总和上限为 {max_steps} 步
   (每个子任务通常消耗 2-5 步, 复杂任务可能更多)。
   请根据预算精简拆解——宁可 4 个子任务全跑完，不要拆 8 个跑到一半被系统截断。
   简单目标 1-2 个子任务即可，中等目标 3-5 个，复杂目标才拆到 6-8 个。
3. global_strategy 必须写: 先分析目标的本质和关键路径, 再给出执行战略。战略要具体可操作,
   例如"先搜索再整理再发布"、"如果搜索失败 2 次则跳过该数据源改用已有知识"。
   不要写空洞的"要认真执行"、"要高质量完成"。
4. tools_hint 写该子任务需要的工具名 (从上方"可用的全部工具"里按 name 选)。
   务必积极填写: 若子任务是"上传到hedgedoc/网页剪藏"就写 web_clip, 是"读写待办"
   就写 todo_add/todo_list/todo_get, 是"搜网页"就写 web_search。不要图省事写空数组——
   写对专用工具可让执行者直接复用, 避免自己写代码逆向摸索浪费大量 token。
   不确定的才写空数组。
5. 每个子任务 prompt 要具体、可执行"""

# Direct execution is intentionally narrow: the Planner may skip a Worker LLM only
# when all arguments are already known from the request or an upstream deterministic result.
PLANNER_PROMPT += """
6. 仅当节点只需调用一个工具、参数已经明确且不需要理解工具结果时，才使用
   execution_mode=tool。分析、研判、搜索后决定下一步、动态引用上游结果等情况必须使用 agent。
   tool 节点必须填写一个真实的 tool_name 和 JSON object tool_arguments；不确定时使用 agent。
"""


class TaskOrchestrator:

    def __init__(self, config: dict, skill_engine: SkillEngine,
                 session_mgr, channels: list = None,
                 ledger: Optional[ExecutionLedger] = None):
        self.config = config
        self.router = ModelRouter(config)
        self.skill_engine = skill_engine
        self.session_mgr = session_mgr
        self.channels = channels or []
        # 共享执行账本 (旁路, 不阻断主流程)
        # TaskOrchestrator 创建父 execution，所有 Worker 子任务挂载其下
        self.ledger = ledger
        self.llm = LLMGateway(self.router, ledger)
        self.model_selector = ModelSelector(config)
        routing = config.get("task_routing", {})
        default_model = config.get("llm", {}).get("default", "")
        self.planner_model = routing.get("planner_model", default_model)
        self.classifier_model = routing.get("classifier_model", default_model)
        self.max_parallel = routing.get("max_parallel_subtasks", 3)
        self.subtask_timeout = routing.get("subtask_timeout_minutes", 15) * 60
        self.max_depth = routing.get("dag_max_depth", 5)
        self.dag_max_steps = routing.get("dag_max_total_steps", 30)
        self.dag_max_tokens = routing.get("dag_max_total_tokens", 200000)
        self.direct_tool_execution = routing.get("direct_tool_execution", False)
        self.request_selector = RequestSelector(skill_engine)
        self.executor = ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="OrchWorker")
        print(f"  [ORCH] 初始化完成 planner={self.planner_model} classifier={self.classifier_model} parallel={self.max_parallel} max_steps={self.dag_max_steps} max_tokens={self.dag_max_tokens}")

    # ==================================================================
    #  Phase 1: 拆解
    # ==================================================================
    def _resolve_model(self, model_key: str) -> str:
        """Resolve config key name to actual API model name"""
        cfg = self.router.models_cfg.get(model_key, {})
        return cfg.get("model", model_key)

    def _plan(self, goal: str, max_steps: int = None,
              parent_execution_id: str = "", session_key: str = "",
              execution_policy: ExecutionPolicy = None,
              budget: ExecutionBudget = None) -> tuple:
        """返回 (subtasks: list[Subtask], global_strategy: str)"""
        if max_steps is None:
            max_steps = self.dag_max_steps
        policy = execution_policy or ExecutionPolicy()
        selector = getattr(self, "model_selector", ModelSelector(self.config))
        decision = selector.select("planner", policy=policy)
        planner_model = decision.model
        print(
            f"  [ORCH:PLAN] 规划中... model={planner_model} "
            f"reason={decision.reason}"
        )
        if budget is not None and not budget.can_start():
            raise RuntimeError("任务预算不足，无法启动 Planner")
        planner_max_tokens = 8192
        if budget is not None:
            planner_max_tokens = max(
                1, min(planner_max_tokens, budget.snapshot().remaining_tokens)
            )
        planner_invoker = self.router.get_invoker(
            planner_model, max_tokens=planner_max_tokens,
            temperature=0.2, timeout=120.0
        )
        if not planner_invoker:
            if policy.model_lock == ModelLock.HARD:
                raise RuntimeError(f"用户锁定模型当前不可用: {planner_model}")
            fallback_model = self.config.get("llm", {}).get("default", "")
            if fallback_model and fallback_model != planner_model:
                planner_model = fallback_model
                planner_invoker = self.router.get_invoker(
                    planner_model, max_tokens=planner_max_tokens,
                    temperature=0.2, timeout=120.0
                )
        if not planner_invoker:
            raise RuntimeError("Planner model is not available")

        actual_model = planner_invoker.model_name

        selection = self.request_selector.select(goal)
        selector_enabled = os.environ.get("LITE_AGENT_SELECTOR_ENABLED") == "1"
        if selector_enabled and selection.names is not None:
            all_tools = self.skill_engine.get_schemas_by_names(selection.names)
        else:
            all_tools = self.skill_engine.get_all_schemas()
        print(
            f"  [ORCH:PLAN] tools={len(all_tools)} selector={selection.confidence} "
            f"enabled={selector_enabled}"
        )
        tools_desc_lines = []
        for t in all_tools:
            fn = t["function"]
            tools_desc_lines.append(f"- {fn['name']}: {fn['description']}")
        tools_desc = "\n".join(tools_desc_lines)

        prompt = PLANNER_PROMPT.format(goal=goal, tools_desc=tools_desc,
                                       max_steps=max_steps)

        attempted = False
        response = None
        try:
            start_t = time.time()
            print(f"  🧠 [LLM Request] 角色: Planner, 模型: {actual_model}")
            gateway = getattr(
                self, "llm", LLMGateway(self.router, getattr(self, "ledger", None))
            )
            attempted = True
            response = gateway.invoke_sync(
                [{"role": "user", "content": prompt}],
                model=planner_model, invoker=planner_invoker,
                role="orchestrator_planner",
                provider=self.router.get_driver(planner_model),
                session_key=session_key or getattr(self, "_active_session_key", ""),
                parent_execution_id=(parent_execution_id or
                                     getattr(self, "_active_parent_execution_id", "")),
                source=ExecutionSource.ORCHESTRATOR,
                max_tokens=planner_max_tokens,
                timeout=120.0,
            )
            print(f"  ✅ [LLM Response] 耗时: {time.time()-start_t:.2f}s, Tokens: {response['usage_total']}")
            if response["finish_reason"] == "length":
                print(f"  ⚠️ Planner 输出达到 max_tokens 截断 (length)，直接触发降级")
                raise ValueError("JSON output truncated due to max_tokens limit")

            raw = response["content"]
            parsed = self._parse_json(raw)
            global_strategy = parsed.get("global_strategy", "")
            subtasks = []
            for item in parsed.get("subtasks", []):
                st_type = SubtaskType(item.get("type", "text"))
                subtasks.append(Subtask(
                    id=item.get("id", f"sub_{uuid.uuid4().hex[:6]}"),
                    name=item["name"],
                    type=st_type,
                    prompt=item.get("prompt", ""),
                    depends_on=item.get("depends_on", []),
                    tools=item.get("tools_hint", []),
                    execution_mode=item.get("execution_mode", "agent"),
                    tool_name=item.get("tool_name", ""),
                    tool_arguments=item.get("tool_arguments", {}),
                ))
            print(f"  [ORCH:PLAN] 拆解完成: {len(subtasks)} 个子任务, strategy={len(global_strategy)} chars")
            return subtasks, global_strategy
        except Exception as e:
            if policy.model_lock == ModelLock.HARD:
                raise
            traceback.print_exc()
            print(f"  ⚠️ 规划失败, 降级为单任务: {e}")
            return [Subtask(
                id="sub_0", name=goal[:40], type=SubtaskType.TEXT,
                prompt=goal, tools=[]
            )], ""
        finally:
            if budget is not None and attempted:
                budget.consume(
                    "planner", steps=1,
                    tokens=int((response or {}).get("usage_total", 0) or 0),
                )

    def _parse_json(self, raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1])
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"  ⚠️ JSON 解析失败: {e}")
            raise ValueError(f"JSONDecodeError: {e}")

    # ==================================================================
    #  Phase 2: 分类 + 路由
    # ==================================================================
    def _classify_and_route(self, subtasks: list[Subtask],
                            execution_policy: ExecutionPolicy = None):
        print(f"  [ORCH:ROUTE] 模型路由中... {len(subtasks)} 个子任务")
        policy = execution_policy or ExecutionPolicy()
        selector = getattr(self, "model_selector", ModelSelector(self.config))
        valid_names = self.skill_engine.get_all_names()
        for s in subtasks:
            decision = selector.select(
                "worker", subtask_type=s.type.value, policy=policy,
                preferred_model=s.assigned_model,
            )
            model_name = decision.model
            tool_filter = selector.route_tools(s.type.value)
            s.assigned_model = model_name
            s.model_reason = decision.reason
            s.fallback_models = list(decision.fallback_models)
            # 工具分配: route_rule 的类型级工具集 与 planner 的 tools_hint 取并集,
            # 而非用 route_rule 覆盖 tools_hint。否则 "上传hedgedoc" 被 classify 成 code
            # 后只剩 ops_workspace_run, planner 给的 web_clip 等专用工具丢失,
            # worker 只能写代码自己逆向摸索 (曾烧百万 token)。并集让两者都满足。
            if tool_filter or s.tools:
                merged = list(tool_filter or [])
                for t in (s.tools or []):
                    if t not in merged:
                        merged.append(t)
                # 校验: 剔除 planner/route_rule 里不存在于技能注册表的假名
                # (planner 常凭语义脑补 "ops_security_audit" 等不存在的技能名,
                #  假名占 allowlist 名额却调不动, 还可能让 worker 误判缺工具)。
                # 同时 route_rule 的工具也校验 (防 config 配错)。
                invalid = [t for t in merged if t not in valid_names]
                if invalid:
                    print(f"  ⚠️ [{s.id}] 剔除非注册技能名: {invalid}")
                    merged = [t for t in merged if t in valid_names]
                s.tools = merged
            tools_str = f" tools={s.tools}" if s.tools else ""
            print(
                f"  [ORCH:ROUTE]   {s.id} type={s.type.value} → "
                f"model={model_name} reason={decision.reason}{tools_str}"
            )

    # ==================================================================
    #  Phase 3: 调度执行
    # ==================================================================
    def execute(self, goal: str, session_key: str,
                progress_callback: Optional[Callable] = None,
                task_id: str = None,
                step_override: int = None,
                token_override: int = None,
                parallel_override: int = None,
                wall_seconds_override: int = None,
                planned_subtasks: list = None,
                planned_strategy: str = "",
                execution_policy: ExecutionPolicy = None) -> str:
        task_id = task_id or uuid.uuid4().hex[:8]
        execution_policy = execution_policy or ExecutionPolicy()

        # Explicit call arguments win over the request policy; config remains
        # the fallback.  Zero/negative values are normalized by ExecutionBudget.
        effective_max_steps = (
            step_override if step_override is not None
            else execution_policy.max_steps
            if execution_policy.max_steps is not None
            else self.dag_max_steps
        )
        effective_max_tokens = (
            token_override if token_override is not None
            else execution_policy.max_total_tokens
            if execution_policy.max_total_tokens is not None
            else self.dag_max_tokens
        )
        requested_parallel = (
            parallel_override if parallel_override is not None
            else execution_policy.max_parallel_tasks
            if execution_policy.max_parallel_tasks is not None
            else self.max_parallel
        )
        effective_parallel = min(
            self.max_parallel,
            max(1, int(requested_parallel)),
        )
        effective_wall_seconds = (
            wall_seconds_override if wall_seconds_override is not None
            else execution_policy.max_wall_seconds
        )
        deadline = (
            time.monotonic() + max(1, int(effective_wall_seconds))
            if effective_wall_seconds is not None else None
        )
        budget = ExecutionBudget(effective_max_steps, effective_max_tokens)
        effective_max_steps = budget.max_steps
        effective_max_tokens = budget.max_tokens
        if step_override:
            print(f"  🔓 用户提升步数预算: {self.dag_max_steps} → {step_override}")

        print(f"\n{'='*60}")
        print(f"🎯 编排任务 [{task_id}]: {goal[:60]}")
        print(f"{'='*60}")

        # ---- 启动父 execution (供所有 Worker 子任务挂载) ----
        parent_execution_id = ""
        if self.ledger is not None:
            planner_decision = self.model_selector.select(
                "planner", policy=execution_policy
            )
            orch_ctx = ExecutionContext(
                actor_id=session_key,
                actor_type=ActorType.USER,
                source=ExecutionSource.ORCHESTRATOR,
                session_key=session_key,
                max_steps=effective_max_steps,
                token_budget=effective_max_tokens,
            )
            parent_exec = self.ledger.start(
                orch_ctx, model_name=planner_decision.model,
                provider="orchestrator", stream_mode=False,
            )
            parent_execution_id = parent_exec.id

        # 默认终态: 任何未在正常路径显式覆盖的出口都按异常失败处理。
        # 正常路径会在 return 前覆盖为 succeeded / 具体失败原因。
        parent_status = "failed"
        parent_reason = "orchestrator_exception"

        try:
            self._active_parent_execution_id = parent_execution_id
            self._active_session_key = session_key
            self.session_mgr.save_subtask_dag(session_key, task_id,
                json.dumps({"global_strategy": "", "subtasks": []}, ensure_ascii=False), "planning")

            if planned_subtasks:
                subtasks, global_strategy = planned_subtasks, planned_strategy
                print(f"  [ORCH:PLAN] 使用已审批 TaskSpec 计划: {len(subtasks)} 个子任务")
            else:
                subtasks, global_strategy = self._plan(
                    goal, max_steps=effective_max_steps,
                    execution_policy=execution_policy,
                    budget=budget,
                )
            if not subtasks:
                parent_status, parent_reason = "failed", "planning_failed"
                return "❌ 任务规划失败，无法拆解目标"

            if global_strategy:
                print(f"  🧭 全局战略: {global_strategy[:120]}...")

            self._classify_and_route(
                subtasks, execution_policy=execution_policy
            )

            # Plan B: Fail-Fast — 规划期估算步数，仅拦截明显离谱的规划 (1.5x 弹性)
            # 因为 Planner 已感知预算并主动精简，运行期还有硬截断兜底，规划期不充当二次裁判。
            # 只有估算远超预算 (如 30 预算拆 10+ 子任务) 才拦截，避免否决 Planner 的紧凑规划。
            estimated = len(subtasks) * 5
            failfast_threshold = effective_max_steps * 1.5
            if estimated > failfast_threshold:
                print(f"  ⚠️ Fail-Fast: 预计 {estimated} 步 > 浮动阈值 {failfast_threshold:.0f} 步, 拒绝执行")
                parent_status, parent_reason = "failed", "budget_rejected"
                return (
                    f"⚠️ **任务预算不足，已拦截**\n\n"
                    f"该任务拆解为 **{len(subtasks)}** 个子任务，"
                    f"粗略预计需约 **{estimated}** 步 LLM 交互，"
                    f"远超当前预算 **{effective_max_steps}** 步（浮动阈值 {failfast_threshold:.0f} 步）。\n\n"
                    f"🔧 **解决方案**: 在指令末尾添加 `[steps={estimated + 10}]` "
                    f"重新下发，即可获得足够的步数配额。\n\n"
                    f"> 原指令: {goal[:100]}{'...' if len(goal) > 100 else ''}"
                )

            for s in subtasks:
                print(f"  📋 {s.id} [{s.type.value}] → {s.assigned_model} : {s.name}")

            dag = SubtaskDAG(subtasks, global_strategy=global_strategy, max_depth=self.max_depth)

            def log_event(msg: str):
                print(msg)
                dag.add_log(msg)
                self._persist_dag(session_key, task_id, dag, force=False)

            log_event(f"  [ORCH:PLAN] 拆解完成: {len(subtasks)} 个子任务, strategy={len(global_strategy)} chars")
            if global_strategy:
                log_event(f"  🧭 全局战略: {global_strategy}")

            for s in subtasks:
                tools_str = f" tools={s.tools}" if s.tools else ""
                log_event(f"  [ORCH:ROUTE] {s.id} type={s.type.value} → model={s.assigned_model}{tools_str}")
                log_event(f"  📋 {s.id} [{s.type.value}] → {s.assigned_model} : {s.name}")

            self._persist_dag(session_key, task_id, dag, force=True)

            while not dag.is_all_done():
                if deadline is not None and time.monotonic() >= deadline:
                    log_event("  ⚠️ DAG 总执行时间预算耗尽")
                    for s in dag.subtasks.values():
                        if s.status in (SubtaskStatus.PENDING, SubtaskStatus.RUNNING):
                            s.status = SubtaskStatus.SKIPPED
                            s.error = "总执行时间预算耗尽"
                    break
                # Planner / Workers / Aggregator share one request-scoped budget.
                current_budget = budget.snapshot()
                total_steps = current_budget.used_steps
                total_tokens = current_budget.used_tokens

                if total_steps >= effective_max_steps:
                    log_event(f"  ⚠️ DAG 全局步数预算耗尽 ({total_steps}/{effective_max_steps})")
                    for s in dag.subtasks.values():
                        if s.status == SubtaskStatus.PENDING:
                            s.status = SubtaskStatus.SKIPPED
                            s.error = f"全局步数预算耗尽，未执行 (已用 {total_steps} 步)"
                    break

                if total_tokens >= effective_max_tokens:
                    log_event(f"  ⚠️ DAG 全局 Token 预算耗尽 ({total_tokens}/{effective_max_tokens})")
                    for s in dag.subtasks.values():
                        if s.status == SubtaskStatus.PENDING:
                            s.status = SubtaskStatus.SKIPPED
                            s.error = f"全局 Token 预算耗尽，未执行 (已用 {total_tokens} tokens)"
                    break

                ready = dag.get_ready()

                if not ready and not dag.is_all_done():
                    for sid, s in dag.subtasks.items():
                        if s.status == SubtaskStatus.PENDING:
                            unmet = [d for d in s.depends_on
                                     if dag.subtasks[d].status not in
                                     (SubtaskStatus.DONE, SubtaskStatus.SKIPPED)]
                            log_event(f"  ⏳ {sid} 等待依赖: {unmet}")
                    break

                if not ready:
                    break

                batch = ready[:effective_parallel]
                # Split the remaining global budget across this parallel batch.
                # A single long local-model task can use the full allowance;
                # concurrent workers cannot each claim the whole task budget.
                worker_max_steps, worker_token_budget = budget.worker_share(
                    len(batch), reserve_steps=1
                )
                if worker_max_steps < 1 or worker_token_budget < 1:
                    log_event(
                        "  ⚠️ DAG 全局预算仅够生成总结，未启动新 Worker"
                    )
                    for s in dag.subtasks.values():
                        if s.status == SubtaskStatus.PENDING:
                            s.status = SubtaskStatus.SKIPPED
                            s.error = "全局预算不足，未执行"
                    break
                futures = []
                results_lock = threading.Lock()
                results = {}

                for subtask in batch:
                    subtask.status = SubtaskStatus.RUNNING
                    subtask.started_at = time.time()
                    log_event(f"  ▶ {subtask.id} [{subtask.type.value}] 开始执行")

                self._persist_dag(session_key, task_id, dag, force=True)

                for subtask in batch:
                    upstream = {}
                    for dep in subtask.depends_on:
                        dep_node = dag.subtasks.get(dep)
                        if dep_node and dep_node.result:
                            upstream[dep] = {
                                "result": dep_node.result,
                                "tool_results": dep_node.tool_results or []
                            }

                    future = self.executor.submit(
                        self._run_single_subtask,
                        subtask, upstream, results, results_lock, goal, global_strategy, log_event,
                        parent_execution_id, worker_max_steps,
                        worker_token_budget,
                    )
                    futures.append(future)

                wait_timeout = self.subtask_timeout
                if deadline is not None:
                    wait_timeout = max(0, min(wait_timeout, deadline - time.monotonic()))
                wait(futures, timeout=wait_timeout)

                for subtask in batch:
                    if subtask.id not in results:
                        results[subtask.id] = {
                            "result": "", "tool_results": [], "status": "failed",
                            "error": "子任务执行超时", "token_usage": subtask.token_usage,
                            "steps_used": subtask.steps_used,
                        }

                for sid, result in results.items():
                    node = dag.subtasks.get(sid)
                    if node:
                        node.result = result["result"]
                        node.tool_results = result.get("tool_results", [])
                        node.status = SubtaskStatus(result["status"])
                        node.error = result.get("error", "")
                        node.token_usage = result.get("token_usage", 0)
                        node.steps_used = result.get("steps_used", 0)
                        budget.consume(
                            "worker", steps=node.steps_used,
                            tokens=node.token_usage,
                        )

                if dag.has_failure():
                    failed = [sid for sid, s in dag.subtasks.items()
                              if s.status == SubtaskStatus.FAILED]
                    for fid in failed:
                        dag.mark_downstream_skipped(fid)

                self._persist_dag(session_key, task_id, dag, force=True)

                if progress_callback:
                    try:
                        progress_callback(dag.progress())
                    except Exception:
                        pass

                log_event(f"  📊 进度: {dag.progress()}")

            log_event(f"  ✅ 编排任务 [{task_id}] 所有子任务执行完毕")
            log_event(f"  [ORCH:AGGR] 正在生成最终总结报告...")
            final_result = self._aggregate(
                dag, goal, execution_policy=execution_policy, budget=budget,
            )
            dag.final_result = final_result or "(总结完成，无额外返回内容)"
            dag.is_aggregated = True
            self._persist_dag(session_key, task_id, dag, force=True)
            log_event(f"  ✅ 总结报告生成完毕")
            usage = budget.snapshot()
            log_event(
                f"  💰 总预算: steps={usage.used_steps}/{usage.max_steps} "
                f"tokens={usage.used_tokens}/{usage.max_tokens} "
                f"roles={budget.usage_by_role()}"
            )

            # 正常完成: 根据子任务整体结果决定终态
            all_done = all(s.status in (SubtaskStatus.DONE, SubtaskStatus.SKIPPED)
                           for s in dag.subtasks.values())
            any_failed = any(s.status == SubtaskStatus.FAILED
                             for s in dag.subtasks.values())
            if any_failed and not all_done:
                parent_status, parent_reason = "failed", "subtask_failed"
            else:
                parent_status, parent_reason = "succeeded", "orchestrated"

            return dag.final_result
        except Exception:
            parent_status, parent_reason = "failed", "orchestrator_exception"
            raise
        finally:
            # 集中收尾: 任何出口 (正常 return / 提前 return / 异常) 都会结束父 execution。
            # finish() 对已是终态的记录是空操作，故重复调用安全。
            if self.ledger is not None and parent_execution_id:
                self.ledger.finish(parent_execution_id,
                                   status=parent_status,
                                   terminal_reason=parent_reason)

    def _run_single_subtask(self, subtask: Subtask, upstream: dict,
                            results: dict, lock: threading.Lock,
                            goal: str = "", global_strategy: str = "",
                            log_callback: Callable = None,
                            parent_execution_id: str = "",
                            worker_max_steps: int = None,
                            worker_token_budget: int = None):
        try:
            if self._can_execute_directly(subtask):
                return self._run_direct_tool_subtask(
                    subtask, results, lock, log_callback, parent_execution_id
                )
            self._log_and_persist(f"  [WORKER:{subtask.id}] 启动 model={subtask.assigned_model} allowlist={subtask.tools[:3] if subtask.tools else 'all'}...", log_callback)

            fallback_attempted = False
            try:
                worker = self._make_worker(
                    subtask, subtask.assigned_model, log_callback,
                    max_steps=worker_max_steps,
                    token_budget=worker_token_budget,
                )
                outcome = worker.run(
                    subtask, upstream, goal=goal,
                    global_strategy=global_strategy,
                    parent_execution_id=parent_execution_id,
                )
            except Exception as primary_error:
                if not subtask.fallback_models:
                    raise
                self._log_and_persist(
                    f"  ⚠️ {subtask.id} 主模型不可用: {primary_error}",
                    log_callback,
                )
                fallback_attempted = True
                outcome = self._run_worker_fallbacks(
                    subtask, upstream, goal, global_strategy,
                    log_callback, parent_execution_id,
                    worker_max_steps, worker_token_budget,
                )
                if outcome is None:
                    raise
            if outcome.status != "done":
                if (
                    outcome.terminal_reason == "model_error"
                    and not fallback_attempted
                ):
                    fallback = self._run_worker_fallbacks(
                        subtask, upstream, goal, global_strategy,
                        log_callback, parent_execution_id,
                        worker_max_steps, worker_token_budget,
                    )
                    if fallback is not None:
                        outcome = fallback
                if outcome.status != "done":
                    raise RuntimeError(
                        f"{outcome.terminal_reason}: {outcome.content}"
                    )
            subtask.finished_at = time.time()

            with lock:
                results[subtask.id] = {
                    "result": outcome.content,
                    "tool_results": outcome.tool_results,
                    "status": "done",
                    "token_usage": subtask.token_usage,
                    "steps_used": subtask.steps_used,
                }
            self._log_and_persist(f"  [WORKER:{subtask.id}] 完成 steps={subtask.steps_used} tokens={subtask.token_usage} result_len={len(outcome.content)}", log_callback)
        except Exception as e:
            traceback.print_exc()
            error_text = str(e)
            self._log_and_persist(f"  ❌ {subtask.id} 失败: {error_text}", log_callback)

            subtask.finished_at = time.time()
            with lock:
                results[subtask.id] = {
                    "result": "",
                    "tool_results": [],
                    "status": "failed",
                    "error": error_text,
                    "token_usage": subtask.token_usage,
                    "steps_used": subtask.steps_used,
                }

    def _make_worker(self, subtask: Subtask, model_name: str,
                     log_callback: Callable = None,
                     max_steps: int = None,
                     token_budget: int = None) -> WorkerAgent:
        model_cfg = dict(self.router.models_cfg.get(model_name, {}))
        if not model_cfg:
            raise RuntimeError(f"Worker model is not configured: {model_name}")
        if max_steps is None:
            max_steps = getattr(
                self, "_active_worker_max_steps", model_cfg.get("max_steps", 8)
            )
        if token_budget is None:
            token_budget = getattr(self, "_active_token_budget", None)
        model_cfg["max_steps"] = max(1, int(max_steps))
        invoker = self.router.get_invoker(model_name)
        if invoker is None:
            raise RuntimeError(f"Worker model is not available: {model_name}")
        return WorkerAgent(
            name=f"Worker-{subtask.id}",
            client=None,
            model_name=invoker.model_name,
            model_cfg=model_cfg,
            skill_engine=self.skill_engine,
            tools_allowlist=subtask.tools if subtask.tools else None,
            driver=self.router.get_driver(model_name),
            log_callback=log_callback,
            ledger=self.ledger,
            token_budget=token_budget,
            invoker=invoker,
        )

    def _run_worker_fallbacks(self, subtask: Subtask, upstream: dict,
                              goal: str, global_strategy: str,
                              log_callback: Callable,
                              parent_execution_id: str,
                              worker_max_steps: int = None,
                              worker_token_budget: int = None) -> Optional[WorkerOutcome]:
        last_outcome = None
        for fallback_model in subtask.fallback_models:
            self._log_and_persist(
                f"  🔄 {subtask.id} fallback → {fallback_model}", log_callback
            )
            try:
                remaining_steps = (
                    max(0, worker_max_steps - subtask.steps_used)
                    if worker_max_steps is not None else None
                )
                remaining_tokens = (
                    max(0, worker_token_budget - subtask.token_usage)
                    if worker_token_budget is not None else None
                )
                if remaining_steps == 0 or remaining_tokens == 0:
                    return WorkerOutcome(
                        "⚠️ Worker fallback 预算已耗尽",
                        status="failed", terminal_reason="token_budget",
                    )
                worker = self._make_worker(
                    subtask, fallback_model, log_callback,
                    max_steps=remaining_steps,
                    token_budget=remaining_tokens,
                )
                last_outcome = worker.run(
                    subtask, upstream, goal=goal,
                    global_strategy=global_strategy,
                    parent_execution_id=parent_execution_id,
                )
                if last_outcome.status == "done":
                    return last_outcome
                if last_outcome.terminal_reason != "model_error":
                    return last_outcome
            except Exception as exc:
                last_outcome = WorkerOutcome(
                    f"❌ Fallback 执行失败: {exc}", status="failed",
                    terminal_reason="worker_exception",
                )
        return last_outcome

    def _can_execute_directly(self, subtask: Subtask) -> bool:
        return bool(
            self.direct_tool_execution
            and subtask.execution_mode == "tool"
            and subtask.tool_name
            and subtask.tool_name in self.skill_engine.get_all_names()
            and isinstance(subtask.tool_arguments, dict)
            and subtask.tool_name in (subtask.tools or [subtask.tool_name])
        )

    def _run_direct_tool_subtask(self, subtask: Subtask, results: dict,
                                 lock: threading.Lock,
                                 log_callback: Callable = None,
                                 parent_execution_id: str = ""):
        """Execute an explicitly planned, self-contained one-tool node without a Worker LLM."""
        ctx = ExecutionContext(
            actor_id=f"Direct-{subtask.id}",
            actor_type=ActorType.WORKER,
            source=ExecutionSource.ORCHESTRATOR,
            allowed_tools=frozenset([subtask.tool_name]),
            session_key=f"direct_{subtask.id}",
            max_steps=1,
            max_output_tokens=0,
            execution_id=parent_execution_id,
        )
        arguments = json.dumps(subtask.tool_arguments, ensure_ascii=False)
        self._log_and_persist(
            f"  ⚡ [DIRECT:{subtask.id}] {subtask.tool_name}", log_callback
        )
        result = self.skill_engine.execute_with_context(
            ctx, subtask.tool_name, arguments
        )
        subtask.steps_used = 0
        subtask.token_usage = 0
        subtask.finished_at = time.time()
        status = "done" if result.ok else "failed"
        with lock:
            results[subtask.id] = {
                "result": result.output,
                "tool_results": [{
                    "name": subtask.tool_name,
                    "args": arguments,
                    "result": result.output,
                }],
                "status": status,
                "error": "" if result.ok else result.output,
                "token_usage": 0,
                "steps_used": 0,
            }

    def _log_and_persist(self, msg: str, log_callback: Callable = None):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    # ==================================================================
    #  Phase 4: 聚合
    # ==================================================================
    def _aggregate(self, dag: SubtaskDAG, goal: str,
                   parent_execution_id: str = "", session_key: str = "",
                   execution_policy: ExecutionPolicy = None,
                   budget: ExecutionBudget = None) -> str:
        print(f"  [ORCH:AGGR] 汇总中... done={dag.progress()['done']}/{dag.progress()['total']}")
        policy = execution_policy or ExecutionPolicy()
        selector = getattr(self, "model_selector", ModelSelector(self.config))
        decision = selector.select("aggregator", policy=policy)
        aggregator_model = decision.model
        aggregator_max_tokens = 4096
        if budget is not None:
            aggregator_max_tokens = max(
                1, min(aggregator_max_tokens, budget.snapshot().remaining_tokens)
            )
        aggregator_invoker = self.router.get_invoker(
            aggregator_model, max_tokens=aggregator_max_tokens,
            temperature=0.3, timeout=60.0
        )
        if not aggregator_invoker:
            if policy.model_lock != ModelLock.HARD:
                fallback_model = self.config.get("llm", {}).get("default", "")
                if fallback_model and fallback_model != aggregator_model:
                    aggregator_model = fallback_model
                    aggregator_invoker = self.router.get_invoker(
                        aggregator_model, max_tokens=aggregator_max_tokens,
                        temperature=0.3, timeout=60.0
                    )

        results_lines = []
        for s in dag.subtasks.values():
            status_label = "✅" if s.status == SubtaskStatus.DONE else (
                "❌" if s.status == SubtaskStatus.FAILED else "⏭️"
            )
            body = s.result or s.error or "(无输出)"
            results_lines.append(f"### {status_label} {s.name} [{s.type.value}]\n{body[:2000]}")

        results_text = "\n\n".join(results_lines)

        if budget is not None and not budget.can_start():
            return (
                f"## 执行报告\n\n{results_text}\n\n"
                "> ⚠️ 总预算已耗尽，未再调用模型生成二次总结。"
            )

        prompt = f"""请根据以下子任务执行结果，对原始目标做最终总结。

原始目标: {goal}

子任务执行结果:

{results_text}

请用 Markdown 格式输出结构化的最终报告，包含:
1. 总体结论
2. 各子任务结果摘要
3. 发现的问题和建议（如有）"""

        attempted = False
        response = None
        try:
            if not aggregator_invoker:
                raise RuntimeError("Aggregator model is not available")
            actual_model = aggregator_invoker.model_name
            start_t = time.time()
            print(
                f"  🧠 [LLM Request] 角色: Aggregator, 模型: {actual_model}, "
                f"reason={decision.reason}"
            )
            gateway = getattr(
                self, "llm", LLMGateway(self.router, getattr(self, "ledger", None))
            )
            attempted = True
            response = gateway.invoke_sync(
                [{"role": "user", "content": prompt}],
                model=aggregator_model, invoker=aggregator_invoker,
                role="orchestrator_aggregator",
                provider=self.router.get_driver(aggregator_model),
                session_key=session_key or getattr(self, "_active_session_key", ""),
                parent_execution_id=(parent_execution_id or
                                     getattr(self, "_active_parent_execution_id", "")),
                source=ExecutionSource.ORCHESTRATOR,
                max_tokens=aggregator_max_tokens,
                timeout=60.0,
            )
            print(f"  ✅ [LLM Response] 耗时: {time.time()-start_t:.2f}s, Tokens: {response['usage_total']}")
            return response["content"]
        except Exception as e:
            traceback.print_exc()
            return f"## 执行报告\n\n{results_text}\n\n> ⚠️ 聚合失败: {e}"
        finally:
            if budget is not None and attempted:
                budget.consume(
                    "aggregator", steps=1,
                    tokens=int((response or {}).get("usage_total", 0) or 0),
                )

    # ==================================================================
    #  持久化
    # ==================================================================
    def _persist_dag(self, session_key: str, task_id: str, dag: SubtaskDAG, force: bool = False):
        now = time.time()
        # 节流逻辑：若非强制落盘(force=True)且距离上次落盘小于 0.3 秒，则仅在内存中追加日志，跳过本次 SQLite 全量序列化
        if not force and hasattr(self, '_last_persist_time') and (now - getattr(self, '_last_persist_time', 0) < 0.3):
            return
        self._last_persist_time = now
        try:
            dag_json = json.dumps(dag.to_dict(), ensure_ascii=False)
            if not dag.is_all_done():
                status = "running"
            elif not dag.is_aggregated:
                status = "summarizing"
            else:
                status = "done"
            self.session_mgr.save_subtask_dag(session_key, task_id, dag_json, status)
        except Exception as e:
            import sys
            print(f"⚠️ [_persist_dag] 持久化失败: {e}", file=sys.stderr)

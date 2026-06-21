import json
import time
import uuid
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Callable, Optional

from openai import OpenAI
from core.model_router import ModelRouter
from core.worker_agent import WorkerAgent
from core.skill_engine import SkillEngine
from core.subtask_dag import Subtask, SubtaskDAG, SubtaskType, SubtaskStatus

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
    }}
  ]
}}

分类规则:
- text: 通用文本处理、翻译、总结、闲聊
- code: 代码生成、调试、审查、脚本编写
- multimodal: 图片理解、OCR、文件视觉分析
- complex_reasoning: 多步推理、数学计算、逻辑分析
- data_analysis: 数据分析、报表生成、趋势判断

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


class TaskOrchestrator:

    def __init__(self, config: dict, skill_engine: SkillEngine,
                 session_mgr, channels: list = None):
        self.config = config
        self.router = ModelRouter(config)
        self.skill_engine = skill_engine
        self.session_mgr = session_mgr
        self.channels = channels or []
        routing = config.get("task_routing", {})
        self.planner_model = routing.get("planner_model", "pro")
        self.classifier_model = routing.get("classifier_model",
                                            config.get("llm", {}).get("default", "flash"))
        self.max_parallel = routing.get("max_parallel_subtasks", 3)
        self.subtask_timeout = routing.get("subtask_timeout_minutes", 15) * 60
        self.max_depth = routing.get("dag_max_depth", 5)
        self.dag_max_steps = routing.get("dag_max_total_steps", 30)
        self.dag_max_tokens = routing.get("dag_max_total_tokens", 200000)
        self.executor = ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="OrchWorker")
        print(f"  [ORCH] 初始化完成 planner={self.planner_model} classifier={self.classifier_model} parallel={self.max_parallel} max_steps={self.dag_max_steps} max_tokens={self.dag_max_tokens}")

    # ==================================================================
    #  Phase 1: 拆解
    # ==================================================================
    def _resolve_model(self, model_key: str) -> str:
        """Resolve config key name to actual API model name"""
        cfg = self.router.models_cfg.get(model_key, {})
        return cfg.get("model", model_key)

    def _plan(self, goal: str, max_steps: int = None) -> tuple:
        """返回 (subtasks: list[Subtask], global_strategy: str)"""
        if max_steps is None:
            max_steps = self.dag_max_steps
        print(f"  [ORCH:PLAN] 规划中... model={self.planner_model}")
        planner_client = self.router.get_client(self.planner_model)
        if not planner_client:
            planner_client = self.router.get_client(
                self.config.get("llm", {}).get("default", "")
            )
            self.planner_model = self.config.get("llm", {}).get("default", "")

        actual_model = self._resolve_model(self.planner_model)

        all_tools = self.skill_engine.get_all_schemas()
        tools_desc_lines = []
        for t in all_tools:
            fn = t["function"]
            tools_desc_lines.append(f"- {fn['name']}: {fn['description']}")
        tools_desc = "\n".join(tools_desc_lines)

        prompt = PLANNER_PROMPT.format(goal=goal, tools_desc=tools_desc,
                                       max_steps=max_steps)

        try:
            start_t = time.time()
            print(f"  🧠 [LLM Request] 角色: Planner, 模型: {actual_model}")
            response = planner_client.chat.completions.create(
                model=actual_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=8192,
                timeout=120.0,
            )
            print(f"  ✅ [LLM Response] 耗时: {time.time()-start_t:.2f}s, Tokens: {response.usage.total_tokens if response.usage else 0}")
            choice = response.choices[0]
            if choice.finish_reason == "length":
                print(f"  ⚠️ Planner 输出达到 max_tokens 截断 (length)，直接触发降级")
                raise ValueError("JSON output truncated due to max_tokens limit")

            raw = choice.message.content
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
                ))
            print(f"  [ORCH:PLAN] 拆解完成: {len(subtasks)} 个子任务, strategy={len(global_strategy)} chars")
            return subtasks, global_strategy
        except Exception as e:
            traceback.print_exc()
            print(f"  ⚠️ 规划失败, 降级为单任务: {e}")
            return [Subtask(
                id="sub_0", name=goal[:40], type=SubtaskType.TEXT,
                prompt=goal, tools=[]
            )], ""

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
    def _classify_and_route(self, subtasks: list[Subtask]):
        print(f"  [ORCH:ROUTE] 模型路由中... {len(subtasks)} 个子任务")
        for s in subtasks:
            model_name, client, tool_filter = self.router.route(s.type.value)
            s.assigned_model = model_name
            # 工具分配: route_rule 的类型级工具集 与 planner 的 tools_hint 取并集,
            # 而非用 route_rule 覆盖 tools_hint。否则 "上传hedgedoc" 被 classify 成 code
            # 后只剩 ops_workspace_run, planner 给的 web_clip 等专用工具丢失,
            # worker 只能写代码自己逆向摸索 (曾烧百万 token)。并集让两者都满足。
            if tool_filter or s.tools:
                merged = list(tool_filter or [])
                for t in (s.tools or []):
                    if t not in merged:
                        merged.append(t)
                s.tools = merged
            tools_str = f" tools={s.tools}" if s.tools else ""
            print(f"  [ORCH:ROUTE]   {s.id} type={s.type.value} → model={model_name}{tools_str}")

    # ==================================================================
    #  Phase 3: 调度执行
    # ==================================================================
    def execute(self, goal: str, session_key: str,
                progress_callback: Optional[Callable] = None,
                task_id: str = None,
                step_override: int = None) -> str:
        task_id = task_id or uuid.uuid4().hex[:8]

        # 用户可通过 [steps=N] 后缀临时提升本次任务预算
        effective_max_steps = step_override if step_override else self.dag_max_steps
        if step_override:
            print(f"  🔓 用户提升步数预算: {self.dag_max_steps} → {step_override}")

        print(f"\n{'='*60}")
        print(f"🎯 编排任务 [{task_id}]: {goal[:60]}")
        print(f"{'='*60}")

        self.session_mgr.save_subtask_dag(session_key, task_id,
            json.dumps({"global_strategy": "", "subtasks": []}, ensure_ascii=False), "planning")

        subtasks, global_strategy = self._plan(goal, max_steps=effective_max_steps)
        if not subtasks:
            return "❌ 任务规划失败，无法拆解目标"

        if global_strategy:
            print(f"  🧭 全局战略: {global_strategy[:120]}...")

        self._classify_and_route(subtasks)

        # Plan B: Fail-Fast — 规划期估算步数，仅拦截明显离谱的规划 (1.5x 弹性)
        # 因为 Planner 已感知预算并主动精简，运行期还有硬截断兜底，规划期不充当二次裁判。
        # 只有估算远超预算 (如 30 预算拆 10+ 子任务) 才拦截，避免否决 Planner 的紧凑规划。
        estimated = len(subtasks) * 5
        failfast_threshold = effective_max_steps * 1.5
        if estimated > failfast_threshold:
            print(f"  ⚠️ Fail-Fast: 预计 {estimated} 步 > 浮动阈值 {failfast_threshold:.0f} 步, 拒绝执行")
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
        self._persist_dag(session_key, task_id, dag)

        while not dag.is_all_done():
            # 全局预算限制检查
            total_steps = sum(s.steps_used for s in dag.subtasks.values())
            total_tokens = sum(s.token_usage for s in dag.subtasks.values())

            if total_steps >= effective_max_steps:
                print(f"  ⚠️ DAG 全局步数预算耗尽 ({total_steps}/{effective_max_steps})")
                for s in dag.subtasks.values():
                    if s.status == SubtaskStatus.PENDING:
                        s.status = SubtaskStatus.SKIPPED
                        s.error = f"全局步数预算耗尽，未执行 (已用 {total_steps} 步)"
                break

            if total_tokens >= self.dag_max_tokens:
                print(f"  ⚠️ DAG 全局 Token 预算耗尽 ({total_tokens}/{self.dag_max_tokens})")
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
                        print(f"  ⏳ {sid} 等待依赖: {unmet}")
                break

            if not ready:
                break

            batch = ready[:self.max_parallel]
            futures = []
            results_lock = threading.Lock()
            results = {}

            for subtask in batch:
                subtask.status = SubtaskStatus.RUNNING
                subtask.started_at = time.time()
                print(f"  ▶ {subtask.id} [{subtask.type.value}] 开始执行")

            self._persist_dag(session_key, task_id, dag)

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
                    subtask, upstream, results, results_lock, goal, global_strategy
                )
                futures.append(future)

            wait(futures, timeout=self.subtask_timeout)

            for sid, result in results.items():
                node = dag.subtasks.get(sid)
                if node:
                    node.result = result["result"]
                    node.tool_results = result.get("tool_results", [])
                    node.status = SubtaskStatus(result["status"])
                    node.error = result.get("error", "")
                    node.token_usage = result.get("token_usage", 0)
                    node.steps_used = result.get("steps_used", 0)

            if dag.has_failure():
                failed = [sid for sid, s in dag.subtasks.items()
                          if s.status == SubtaskStatus.FAILED]
                for fid in failed:
                    dag.mark_downstream_skipped(fid)

            self._persist_dag(session_key, task_id, dag)

            if progress_callback:
                try:
                    progress_callback(dag.progress())
                except Exception:
                    pass

            print(f"  📊 进度: {dag.progress()}")

        print(f"  ✅ 编排任务 [{task_id}] 完成")
        return self._aggregate(dag, goal)

    def _run_single_subtask(self, subtask: Subtask, upstream: dict,
                            results: dict, lock: threading.Lock,
                            goal: str = "", global_strategy: str = ""):
        try:
            print(f"  [WORKER:{subtask.id}] 启动 model={subtask.assigned_model} allowlist={subtask.tools[:3] if subtask.tools else 'all'}...")
            client = self.router.get_client(subtask.assigned_model)
            if not client:
                client = self.router.get_client(
                    self.config.get("llm", {}).get("default", "")
                )

            model_cfg = self.router.models_cfg.get(
                subtask.assigned_model,
                self.router.models_cfg.get(
                    self.config.get("llm", {}).get("default", ""), {}
                )
            )

            worker = WorkerAgent(
                name=f"Worker-{subtask.id}",
                client=client,
                model_name=subtask.assigned_model,
                model_cfg=model_cfg,
                skill_engine=self.skill_engine,
                tools_allowlist=subtask.tools if subtask.tools else None,
                provider=self.router.get_provider(subtask.assigned_model),
            )
            result_text, tool_results = worker.run(subtask, upstream,
                                     goal=goal, global_strategy=global_strategy)
            subtask.finished_at = time.time()

            with lock:
                results[subtask.id] = {
                    "result": result_text,
                    "tool_results": tool_results,
                    "status": "done",
                    "token_usage": subtask.token_usage,
                    "steps_used": subtask.steps_used,
                }
            print(f"  [WORKER:{subtask.id}] 完成 steps={subtask.steps_used} tokens={subtask.token_usage} result_len={len(result_text)}")
        except Exception as e:
            traceback.print_exc()
            error_text = str(e)
            print(f"  ❌ {subtask.id} 失败: {error_text}")

            fb = self.router.get_fallback(subtask.assigned_model)
            if fb and fb[0] != subtask.assigned_model:
                fb_name, fb_client = fb
                print(f"  🔄 {subtask.id} fallback → {fb_name}")
                try:
                    fb_cfg = self.router.models_cfg.get(fb_name, {})
                    worker_fb = WorkerAgent(
                        name=f"Worker-{subtask.id}-fb",
                        client=fb_client,
                        model_name=fb_name,
                        model_cfg=fb_cfg,
                        skill_engine=self.skill_engine,
                        tools_allowlist=subtask.tools if subtask.tools else None,
                        provider=self.router.get_provider(fb_name),
                    )
                    result_text, tool_results = worker_fb.run(subtask, upstream,
                                                goal=goal, global_strategy=global_strategy)
                    subtask.finished_at = time.time()
                    with lock:
                        results[subtask.id] = {
                            "result": result_text,
                            "tool_results": tool_results,
                            "status": "done",
                            "token_usage": subtask.token_usage,
                            "steps_used": subtask.steps_used,
                        }
                    return
                except Exception:
                    traceback.print_exc()

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

    # ==================================================================
    #  Phase 4: 聚合
    # ==================================================================
    def _aggregate(self, dag: SubtaskDAG, goal: str) -> str:
        print(f"  [ORCH:AGGR] 汇总中... done={dag.progress()['done']}/{dag.progress()['total']}")
        aggregator_client = self.router.get_client(self.planner_model)
        if not aggregator_client:
            aggregator_client = self.router.get_client(
                self.config.get("llm", {}).get("default", "")
            )

        results_lines = []
        for s in dag.subtasks.values():
            status_label = "✅" if s.status == SubtaskStatus.DONE else (
                "❌" if s.status == SubtaskStatus.FAILED else "⏭️"
            )
            body = s.result or s.error or "(无输出)"
            results_lines.append(f"### {status_label} {s.name} [{s.type.value}]\n{body[:2000]}")

        results_text = "\n\n".join(results_lines)

        prompt = f"""请根据以下子任务执行结果，对原始目标做最终总结。

原始目标: {goal}

子任务执行结果:

{results_text}

请用 Markdown 格式输出结构化的最终报告，包含:
1. 总体结论
2. 各子任务结果摘要
3. 发现的问题和建议（如有）"""

        try:
            actual_model = self._resolve_model(self.planner_model)
            start_t = time.time()
            print(f"  🧠 [LLM Request] 角色: Aggregator, 模型: {actual_model}")
            response = aggregator_client.chat.completions.create(
                model=actual_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=4096,
                timeout=60.0,
            )
            print(f"  ✅ [LLM Response] 耗时: {time.time()-start_t:.2f}s, Tokens: {response.usage.total_tokens if response.usage else 0}")
            return response.choices[0].message.content
        except Exception as e:
            traceback.print_exc()
            return f"## 执行报告\n\n{results_text}\n\n> ⚠️ 聚合失败: {e}"

    # ==================================================================
    #  持久化
    # ==================================================================
    def _persist_dag(self, session_key: str, task_id: str, dag: SubtaskDAG):
        try:
            dag_json = json.dumps(dag.to_dict(), ensure_ascii=False)
            status = "running" if not dag.is_all_done() else "done"
            self.session_mgr.save_subtask_dag(session_key, task_id, dag_json, status)
        except Exception:
            pass

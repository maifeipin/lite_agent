"""High-value author/reviewer workflow around the deterministic TaskSpec core."""

import copy
import json
from datetime import datetime, timezone
from typing import Optional

from core.model_router import ModelRouter
from core.model_policy import ModelSelector
from core.llm_gateway import LLMGateway
from core.execution import ExecutionSource
from core.task_spec import (
    BASE_POLICY,
    TaskSpecStore,
    content_digest,
    finding,
    new_task_spec,
    normalize_task_spec,
    policy_digest,
    preflight,
)
from core.subtask_dag import Subtask, SubtaskType


AUTHOR_PROMPT = """你负责把用户目标编译成可编辑的 TaskSpec。只输出 JSON object，不要 Markdown。

硬规则由代码执行，你只能补充以下四个顶层字段：task、execution、output、on_failure。
只返回需要修改的最小字段集合，不要复述基础草案中未变化的字段。
不要写入密钥，不要虚构不存在的模型或能力。简单、只读、参数已完全确定的步骤优先 mode=tool；
需要根据上游结果判断或有副作用的步骤使用 mode=agent。普通执行节点优先低成本模型，
高价值模型只用于真正的复杂推理。plan 节点格式：
缺少的必需条件写入 task.required_inputs，格式为
{{"条件名":{{"description":"说明","required":true,"value":"","suggested_value":"可选默认建议"}}}}。
你不能替用户确认 value，必须保持 value 为空；只有存在明显、安全、可修改的默认值时才填写
suggested_value，例如“最近30天”。没有可靠默认值时省略 suggested_value。
{{"id":"step_1","objective":"...","type":"text|code|data_analysis|complex_reasoning",
"depends_on":[],"mode":"tool|agent","capabilities":["web.search"],
"executor":{{"preferred_model":"","model_tier":"low"}},
"tool":{{"name":"仅 tool 节点填写实际工具名","arguments":{{}}}}}}

可用工具如下。若工具能直接读取所需数据，优先生成 mode=tool 节点，不要再把该数据声明为
用户必需输入；带默认值的工具参数可以直接采用默认值或写入 arguments。若现有草案中的
required_inputs 已被工具取代，必须在返回结果中显式写入 required_inputs={{}} 将其移除：
{tools}

用户目标：
{goal}

基础草案：
{draft}

可用模型：{models}
能力映射：{capabilities}
"""


REVIEW_PROMPT = """你是 TaskSpec 高价值复核者。检查计划是否足以完成原始目标、模型能力是否合适、
步骤是否遗漏、预算是否明显不足、验收标准是否可验证。系统硬规则已由代码检查。

运行时事实由代码提供，优先于猜测。不要对已由系统配置满足的条件报缺失，也不要把工具节点
仅负责调用工具误判为缺少结果整理；所有子任务结果都会进入最终 Aggregator。

运行时事实（不含密钥和具体收件地址）：
{runtime_context}

你不能产生不可覆盖的硬阻断。只输出 JSON：
{{
  "passed": true,
  "findings": [{{
    "code": "简短代码",
    "severity": "suggestion|warning|needs_ack",
    "path": "/json/path",
    "message": "说明",
    "resolution": "建议"
  }}]
}}

TaskSpec：
{spec}
"""


def _parse_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("模型输出必须是 JSON object")
    return value


def _merge_generated(base: dict, generated: dict) -> dict:
    """Keep required draft fields when the author model returns a partial object."""
    merged = copy.deepcopy(base)
    for key, value in (generated or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_generated(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class TaskSpecRevisionConflict(ValueError):
    """The editable task changed while a model was preparing an update."""


class TaskSpecOutputTruncated(ValueError):
    """The provider stopped before completing the required JSON object."""


class TaskSpecService:
    def __init__(self, config: dict, skill_engine=None,
                 store: Optional[TaskSpecStore] = None, ledger=None,
                 request_selector=None):
        self.config = config
        self.skill_engine = skill_engine
        self.store = store or TaskSpecStore()
        self.ledger = ledger
        self.router = ModelRouter(config)
        self.model_selector = ModelSelector(config)
        self.llm = LLMGateway(self.router, ledger)
        self.request_selector = request_selector
        task_cfg = config.get("task_specs", {}) or {}
        self.task_cfg = task_cfg
        routing = config.get("task_routing", {}) or {}
        llm = config.get("llm", {}) or {}
        self.author_model = task_cfg.get(
            "author_model", routing.get("planner_model", llm.get("default", ""))
        )
        self.validator_model = task_cfg.get("validator_model", self.author_model)
        self.capability_map = task_cfg.get("capability_map", {}) or {}

    @property
    def capabilities(self) -> set:
        return set(self.capability_map)

    def deterministic_preflight(self, spec: dict) -> dict:
        tools = self.skill_engine.get_all_names() if self.skill_engine is not None else None
        return preflight(spec, self.config, self.capabilities, tools)

    def _review_runtime_context(self) -> dict:
        output_cfg = self.config.get("output_delivery", {}) or {}
        email_cfg = output_cfg.get("email", {}) or {}
        email_configured = bool(
            email_cfg.get("enabled") and str(email_cfg.get("recipient") or "").strip()
        )
        return {
            "output_delivery": {
                "email": {
                    "configured": email_configured,
                    "recipient_source": (
                        "server_configuration" if email_configured else "not_configured"
                    ),
                    "task_required_input_needed": not email_configured,
                },
                "sqlite_archive": {"configured": True},
            },
            "dag_execution": {
                "direct_tool_node": "invokes the approved tool and records its result",
                "final_aggregator": "always receives all node results and formats the final answer",
            },
        }

    @staticmethod
    def _validation(spec: dict, status: str, report: dict,
                    findings: Optional[list] = None) -> dict:
        updated = copy.deepcopy(spec)
        updated["validation"] = {
            "status": status,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "preflight": report,
            "findings": copy.deepcopy(findings or []),
        }
        return updated

    def create_manual(self, goal: str, name: str = "") -> dict:
        spec = new_task_spec(goal, name=name)
        return self.store.save(spec, status="review_required", enabled=False)

    def _save_generation_fallback(self, spec: dict, exc: Exception) -> dict:
        """Keep the interactive workflow usable when the author model fails."""
        error_name = type(exc).__name__
        is_timeout = "timeout" in error_name.lower()
        is_truncated = isinstance(exc, TaskSpecOutputTruncated)
        if is_timeout:
            code = "AUTHOR_MODEL_TIMEOUT"
            message = "高价值生成模型调用超时，已创建可编辑的基础规则。"
        elif is_truncated:
            code = "AUTHOR_OUTPUT_TRUNCATED"
            message = "生成模型输出超过 JSON 长度上限，已创建可编辑的基础规则。"
        else:
            code = "AUTHOR_MODEL_FAILED"
            message = "高价值生成模型暂时不可用，已创建可编辑的基础规则。"
        report = self.deterministic_preflight(spec)
        spec["contract"]["generated_by"] = "deterministic-fallback"
        spec = self._validation(spec, "review_required", report, [finding(
            code, message, path="/execution/plan",
            resolution="请在页面补充执行计划和条件，然后执行高价值复核。",
            severity="warning", overrideable=True, source="runtime",
        )])
        saved = self.store.save(spec, status="review_required", enabled=False)
        saved["generation"] = {
            "status": "fallback",
            "code": code,
            "message": message,
        }
        return saved

    def _author_model_for_request(self, model: str = "") -> str:
        selected = str(model or self.author_model).strip()
        if selected not in self.router.models_cfg:
            raise ValueError(f"TaskSpec author model is not configured: {selected}")
        return selected

    def _author_tools(self, goal: str) -> list:
        if self.skill_engine is None or self.request_selector is None:
            return []
        selected = self.request_selector.select(
            goal, history=[], is_guest=False, max_tools=15
        )
        # TaskSpec authoring is not the runtime's all-tools fallback path.
        # Unknown intent gets no schemas; recognized domains get at most the
        # selector's bounded subset. This keeps author prompt size predictable.
        if not selected.names:
            return []
        schemas = self.skill_engine.get_schemas_by_names(selected.names)
        return [schema.get("function", {}) for schema in schemas]

    def _call_profile(self, model: str) -> dict:
        """Resolve author/reviewer calls from the model's structured profile."""
        profile = self.router.get_call_profile(model, "structured_json") or {}
        max_tokens = int(profile.get("max_tokens", 2048))
        timeout = float(profile.get("timeout", 120.0))
        max_retries = int(profile.get("max_retries", 0))
        invoke_kwargs = copy.deepcopy(profile.get("invoke_kwargs") or {})
        for reserved in ("max_tokens", "timeout", "max_retries"):
            invoke_kwargs.pop(reserved, None)
        return {
            "max_tokens": max(1, max_tokens),
            "timeout": max(1.0, timeout),
            "max_retries": max(0, max_retries),
            "invoke_kwargs": invoke_kwargs,
        }

    def _invoke_author_model(self, prompt: str, spec: dict,
                             selected_model: str) -> dict:
        profile = self._call_profile(selected_model)
        invoker = self.router.get_invoker(
            selected_model, temperature=0.1,
            max_tokens=profile["max_tokens"], timeout=profile["timeout"],
        )
        if invoker is None:
            raise RuntimeError(
                f"TaskSpec author model unavailable: {selected_model}"
            )
        result = self.llm.invoke_sync(
            [{"role": "user", "content": prompt}],
            model=selected_model, invoker=invoker, role="task_spec_author",
            provider=self.router.get_driver(selected_model),
            session_key=f"task_spec:{spec['contract']['task_id']}",
            source=ExecutionSource.API,
            max_tokens=profile["max_tokens"], timeout=profile["timeout"],
            max_retries=profile["max_retries"], **profile["invoke_kwargs"],
        )
        if result.get("finish_reason") == "length":
            raise TaskSpecOutputTruncated(
                f"TaskSpec author output truncated: {selected_model}"
            )
        return _parse_json_object(result["content"])

    def _invoke_author(self, spec: dict, goal: str, model: str = "") -> tuple[dict, str]:
        selected_model = self._author_model_for_request(model)
        prompt = AUTHOR_PROMPT.format(
            goal=goal,
            draft=json.dumps(spec, ensure_ascii=False, indent=2),
            models=json.dumps(sorted(self.router.models_cfg), ensure_ascii=False),
            capabilities=json.dumps(self.capability_map, ensure_ascii=False),
            tools=json.dumps(self._author_tools(goal), ensure_ascii=False, indent=2),
        )
        try:
            return self._invoke_author_model(prompt, spec, selected_model), selected_model
        except Exception as primary_error:
            fallbacks = self.model_selector.fallback_models(
                selected_model, SubtaskType.TEXT.value
            )
            if not fallbacks:
                raise
            fallback_model = fallbacks[0]
            try:
                return self._invoke_author_model(
                    prompt, spec, fallback_model
                ), fallback_model
            except Exception as fallback_error:
                raise fallback_error from primary_error

    @staticmethod
    def _require_confirmation_for_suggestions(spec: dict, generated: dict) -> dict:
        """A model may suggest missing input values, but cannot confirm them."""
        safe = copy.deepcopy(generated)
        base_inputs = (spec.get("task") or {}).get("required_inputs") or {}
        generated_inputs = (safe.get("task") or {}).get("required_inputs")
        if not isinstance(generated_inputs, dict):
            return safe
        for name, detail in generated_inputs.items():
            if not isinstance(detail, dict):
                continue
            base_detail = base_inputs.get(name) if isinstance(base_inputs, dict) else None
            if isinstance(base_detail, dict) and base_detail.get("value") not in (None, "", [], {}):
                detail.pop("value", None)
                continue
            proposed = detail.get("value")
            if proposed not in (None, "", [], {}):
                detail.setdefault("suggested_value", proposed)
                detail["value"] = ""
        return safe

    def _apply_generated(self, spec: dict, generated: dict,
                         generated_by: str,
                         revision: Optional[int] = None) -> tuple[dict, dict, str]:
        updated = copy.deepcopy(spec)
        current_model_policy = copy.deepcopy(
            (spec.get("execution") or {}).get("model_policy") or {}
        )
        generated = self._require_confirmation_for_suggestions(spec, generated)
        for key in ("task", "execution", "output", "on_failure"):
            if key in generated:
                updated[key] = _merge_generated(updated[key], generated[key])
        # The enrichment model authors the rule; it must not override a runtime
        # model that the user explicitly locked in the editor.
        if (current_model_policy.get("user_locked")
                and current_model_policy.get("preferred_model")):
            merged_policy = updated.setdefault("execution", {}).setdefault(
                "model_policy", {}
            )
            merged_policy["preferred_model"] = current_model_policy["preferred_model"]
            merged_policy["user_locked"] = True
            merged_policy["allowed_models"] = copy.deepcopy(
                current_model_policy.get("allowed_models") or []
            )
        # An explicit required_inputs object is authoritative. Recursive merge
        # would otherwise preserve stale blockers after a tool replaces them.
        generated_task = generated.get("task")
        if isinstance(generated_task, dict) and "required_inputs" in generated_task:
            updated["task"]["required_inputs"] = copy.deepcopy(
                generated_task["required_inputs"]
            )
        updated = normalize_task_spec(updated)
        updated["contract"]["generated_by"] = generated_by
        if revision is not None:
            updated["contract"]["revision"] = revision
        digest = content_digest(updated)
        updated["contract"]["content_digest"] = digest
        updated["contract"]["generated_digest"] = digest
        updated["contract"].pop("validated_digest", None)
        report = self.deterministic_preflight(updated)
        status = "draft" if report["status"] == "ready" else "blocked"
        return self._validation(updated, status, report), report, status

    def import_spec(self, uploaded: dict) -> dict:
        """Import an uploaded rule file without trusting its identity or policy."""
        if uploaded.get("policy") != BASE_POLICY:
            raise ValueError("上传规则缺少当前不可变策略，或策略已被修改")
        contract = uploaded.get("contract") or {}
        if contract.get("policy_digest") != policy_digest():
            raise ValueError("上传规则的 policy_digest 与当前系统不一致")
        goal = str((uploaded.get("task") or {}).get("objective") or "").strip()
        if not goal:
            raise ValueError("spec.task.objective is required")
        spec = new_task_spec(goal, name=str((uploaded.get("task") or {}).get("name") or ""))
        for key in ("task", "execution", "output", "on_failure"):
            if key not in uploaded:
                raise ValueError(f"上传规则缺少必需字段: {key}")
            spec[key] = copy.deepcopy(uploaded[key])
        spec = normalize_task_spec(spec)
        spec["contract"]["content_digest"] = content_digest(spec)
        return self.store.save(spec, status="review_required", enabled=False)

    def generate(self, goal: str, name: str = "") -> dict:
        spec = new_task_spec(goal, name=name)
        try:
            generated, selected_model = self._invoke_author(spec, goal)
        except Exception as exc:
            return self._save_generation_fallback(spec, exc)
        spec, report, status = self._apply_generated(
            spec, generated, selected_model
        )
        saved = self.store.save(spec, status=status, enabled=False)
        saved["preflight"] = report
        return saved

    def enrich(self, task_id: str, model: str = "") -> dict:
        """Optionally improve an already-persisted draft without losing user edits."""
        current = self.store.get(task_id)
        if current is None:
            raise KeyError(task_id)
        starting_spec = normalize_task_spec(current["spec"])
        starting_revision = int(starting_spec["contract"].get("revision", 1))
        starting_digest = content_digest(starting_spec)
        goal = str(
            starting_spec["contract"].get("original_goal")
            or starting_spec.get("task", {}).get("objective")
            or ""
        )
        selected_model = self._author_model_for_request(model)
        try:
            generated, selected_model = self._invoke_author(
                starting_spec, goal, selected_model
            )
        except Exception as exc:
            latest = self.store.get(task_id)
            if latest is None:
                raise KeyError(task_id)
            result = copy.deepcopy(latest)
            is_timeout = "timeout" in type(exc).__name__.lower()
            is_truncated = isinstance(exc, TaskSpecOutputTruncated)
            if is_timeout:
                code = "AUTHOR_MODEL_TIMEOUT"
                message = "高价值生成模型调用超时，基础规则已保留，可稍后重试。"
            elif is_truncated:
                code = "AUTHOR_OUTPUT_TRUNCATED"
                message = "生成模型输出超过 JSON 长度上限，基础规则已保留，可改用更精简的模型重试。"
            else:
                code = "AUTHOR_MODEL_FAILED"
                message = "高价值生成模型暂时不可用，基础规则已保留，可稍后重试。"
            result["generation"] = {
                "status": "fallback",
                "code": code,
                "message": message,
            }
            return result

        updated, report, status = self._apply_generated(
            starting_spec, generated, selected_model,
            revision=starting_revision + 1,
        )
        # Serialize only the final compare-and-save. A concurrent user save wins;
        # two enrich requests cannot both overwrite the same starting revision.
        with self.store._lock:
            latest = self.store.get(task_id)
            if latest is None:
                raise KeyError(task_id)
            latest_spec = latest["spec"]
            latest_revision = int(latest_spec.get("contract", {}).get("revision", 1))
            if (latest_revision != starting_revision
                    or content_digest(latest_spec) != starting_digest):
                raise TaskSpecRevisionConflict(
                    "任务在 AI 完善期间已被修改；模型结果未覆盖当前版本，请刷新后重试"
                )
            saved = self.store.save(updated, status=status, enabled=False)
        saved["preflight"] = report
        saved["generation"] = {"status": "completed"}
        return saved

    def update(self, task_id: str, edited_spec: dict) -> dict:
        current = self.store.get(task_id)
        if current is None:
            raise KeyError(task_id)
        if (edited_spec.get("policy") != BASE_POLICY or
                (edited_spec.get("contract") or {}).get("policy_digest") != current["spec"]["contract"].get("policy_digest")):
            raise ValueError("不可变策略字段不能修改")
        spec = normalize_task_spec(edited_spec)
        contract = spec.setdefault("contract", {})
        contract["task_id"] = task_id
        contract["revision"] = int(current["spec"]["contract"].get("revision", 1)) + 1
        contract["original_goal"] = current["spec"]["contract"].get("original_goal", "")
        contract["generated_by"] = current["spec"]["contract"].get("generated_by", "user")
        contract["generated_at"] = current["spec"]["contract"].get("generated_at", "")
        contract.pop("generated_digest", None)
        contract.pop("validated_digest", None)
        contract["content_digest"] = content_digest(spec)
        spec.pop("validation", None)
        return self.store.save(spec, status="review_required", enabled=False)

    def confirm_generated(self, task_id: str) -> dict:
        current = self.store.get(task_id)
        if current is None:
            raise KeyError(task_id)
        spec = current["spec"]
        report = self.deterministic_preflight(spec)
        generated_digest = spec.get("contract", {}).get("generated_digest")
        if report["status"] != "ready" or generated_digest != content_digest(spec):
            spec = self._validation(spec, "blocked", report)
            saved = self.store.save(spec, status="blocked", enabled=False)
            return {"status": "blocked", "preflight": report, "task": saved}
        spec["contract"]["validated_digest"] = content_digest(spec)
        spec = self._validation(spec, "approved", report)
        saved = self.store.save(spec, status="approved", enabled=current["enabled"])
        return {"status": "approved", "preflight": report, "task": saved}

    def _invoke_reviewer_model(self, prompt: str, task_id: str,
                               selected_model: str) -> dict:
        profile = self._call_profile(selected_model)
        invoker = self.router.get_invoker(
            selected_model, temperature=0.1,
            max_tokens=profile["max_tokens"], timeout=profile["timeout"],
        )
        if invoker is None:
            raise RuntimeError(
                f"TaskSpec validator model unavailable: {selected_model}"
            )
        result = self.llm.invoke_sync(
            [{"role": "user", "content": prompt}],
            model=selected_model, invoker=invoker, role="task_spec_validator",
            provider=self.router.get_driver(selected_model),
            session_key=f"task_spec:{task_id}", source=ExecutionSource.API,
            max_tokens=profile["max_tokens"], timeout=profile["timeout"],
            max_retries=profile["max_retries"], **profile["invoke_kwargs"],
        )
        if result.get("finish_reason") == "length":
            raise TaskSpecOutputTruncated(
                f"TaskSpec validator output truncated: {selected_model}"
            )
        return _parse_json_object(result["content"])

    def review(self, task_id: str) -> dict:
        current = self.store.get(task_id)
        if current is None:
            raise KeyError(task_id)
        spec = normalize_task_spec(current["spec"])
        hard_report = self.deterministic_preflight(spec)
        if hard_report["status"] != "ready":
            spec = self._validation(spec, "blocked", hard_report)
            self.store.save(spec, status="blocked", enabled=False)
            return {"status": "blocked", "preflight": hard_report, "findings": []}

        review_spec = copy.deepcopy(spec)
        review_spec.pop("validation", None)
        prompt = REVIEW_PROMPT.format(
            runtime_context=json.dumps(
                self._review_runtime_context(), ensure_ascii=False, indent=2
            ),
            spec=json.dumps(review_spec, ensure_ascii=False, indent=2),
        )
        try:
            reviewed = self._invoke_reviewer_model(
                prompt, task_id, self.validator_model
            )
        except Exception as exc:
            fallbacks = self.model_selector.fallback_models(
                self.validator_model, SubtaskType.TEXT.value
            )
            if fallbacks:
                try:
                    reviewed = self._invoke_reviewer_model(
                        prompt, task_id, fallbacks[0]
                    )
                    exc = None
                except Exception as fallback_error:
                    exc = fallback_error
            if exc is not None:
                error_name = type(exc).__name__
                is_timeout = "timeout" in error_name.lower()
                is_truncated = isinstance(exc, TaskSpecOutputTruncated)
                if is_timeout:
                    code = "VALIDATOR_MODEL_TIMEOUT"
                    message = "高价值复核模型调用超时，请稍后重试。"
                elif is_truncated:
                    code = "VALIDATOR_OUTPUT_TRUNCATED"
                    message = "复核模型输出超过 JSON 长度上限，请改用更精简的模型后重试。"
                else:
                    code = "VALIDATOR_MODEL_FAILED"
                    message = "高价值复核模型暂时不可用，请稍后重试。"
                unavailable = finding(
                    code, message, path="/validation",
                    resolution="任务草案已保留，可稍后再次点击高价值复核。",
                    severity="warning", overrideable=False, source="runtime",
                )
                spec = self._validation(
                    spec, "review_required", hard_report, [unavailable]
                )
                self.store.save(spec, status="review_required", enabled=False)
                return {
                    "status": "review_required", "preflight": hard_report,
                    "findings": [unavailable],
                }
        findings = []
        allowed_severity = {"suggestion", "warning", "needs_ack"}
        for raw in reviewed.get("findings") or []:
            severity = raw.get("severity", "warning")
            if severity not in allowed_severity:
                severity = "needs_ack"
            findings.append(finding(
                str(raw.get("code") or "VALIDATOR_NOTE"),
                str(raw.get("message") or "复核模型提出了一项注意事项"),
                path=str(raw.get("path") or ""),
                resolution=str(raw.get("resolution") or ""),
                severity=severity,
                overrideable=True,
                source="validator",
            ))
        needs_ack = any(item["severity"] == "needs_ack" for item in findings)
        passed = bool(reviewed.get("passed", False)) and not needs_ack
        if passed:
            spec = copy.deepcopy(spec)
            spec["contract"]["validated_digest"] = content_digest(spec)
            spec = self._validation(spec, "approved", hard_report, findings)
            self.store.save(spec, status="approved", enabled=False)
            status = "approved"
        else:
            spec = self._validation(spec, "needs_ack", hard_report, findings)
            self.store.save(spec, status="needs_ack", enabled=False)
            status = "needs_ack"
        return {"status": status, "preflight": hard_report, "findings": findings}

    def acknowledge(self, task_id: str, rationale: str = "") -> dict:
        current = self.store.get(task_id)
        if current is None:
            raise KeyError(task_id)
        if current["status"] != "needs_ack":
            raise ValueError("当前任务没有待确认的复核建议")
        spec = copy.deepcopy(current["spec"])
        spec["contract"]["validated_digest"] = content_digest(spec)
        spec["contract"]["user_acknowledgement"] = rationale.strip()
        validation = spec.setdefault("validation", {})
        validation["status"] = "approved_by_user"
        validation["acknowledged_at"] = datetime.now(timezone.utc).isoformat()
        validation["acknowledgement"] = rationale.strip()
        return self.store.save(spec, status="approved", enabled=False)

    def build_subtasks(self, spec: dict) -> list:
        """Translate the deliberately small TaskSpec plan into the existing DAG type."""
        execution = spec.get("execution") or {}
        global_policy = execution.get("model_policy") or {}
        configured_models = set(self.router.models_cfg)
        tiers = (self.config.get("task_specs", {}) or {}).get("model_tiers", {}) or {}
        subtasks = []
        for index, node in enumerate(execution.get("plan") or []):
            node_type = node.get("type", "text")
            try:
                subtask_type = SubtaskType(node_type)
            except ValueError:
                subtask_type = SubtaskType.TEXT
            caps = node.get("capabilities") or []
            tools = []
            for capability in caps:
                mapped = self.capability_map.get(capability, [])
                if isinstance(mapped, dict):
                    mapped = mapped.get("tools", [])
                for tool_name in mapped:
                    if tool_name not in tools:
                        tools.append(tool_name)
            tool = node.get("tool") or {}
            if tool.get("name") and tool["name"] not in tools:
                tools.append(tool["name"])

            executor = node.get("executor") or {}
            preferred = executor.get("preferred_model") or global_policy.get("preferred_model") or ""
            if not preferred:
                tier = executor.get("model_tier") or global_policy.get("recommended_tier", "low")
                preferred = next(
                    (name for name in tiers.get(tier, []) if name in configured_models), ""
                )
            subtasks.append(Subtask(
                id=str(node.get("id") or f"step_{index + 1}"),
                name=str(node.get("objective") or f"步骤 {index + 1}")[:80],
                type=subtask_type,
                prompt=str(node.get("objective") or ""),
                depends_on=list(node.get("depends_on") or []),
                tools=tools,
                assigned_model=preferred if preferred in configured_models else "",
                execution_mode="tool" if node.get("mode") == "tool" else "agent",
                tool_name=str(tool.get("name") or ""),
                tool_arguments=tool.get("arguments") if isinstance(tool.get("arguments"), dict) else {},
            ))
        return subtasks

    @staticmethod
    def build_execution_policy(spec: dict):
        """Translate an approved TaskSpec model contract into runtime policy."""
        from core.model_policy import ExecutionPolicy, ModelLock

        execution = spec.get("execution") or {}
        model_policy = execution.get("model_policy") or {}
        budget = execution.get("budget") or {}
        preferred = str(model_policy.get("preferred_model") or "")
        if preferred and model_policy.get("user_locked"):
            lock = ModelLock.HARD
        elif preferred:
            lock = ModelLock.PREFERRED
        else:
            lock = ModelLock.AUTO
        return ExecutionPolicy(
            requested_model=preferred,
            model_lock=lock,
            lock_source="task_spec",
            allowed_models=tuple(model_policy.get("allowed_models") or ()),
            max_steps=int(budget.get("max_steps", 20)),
            max_total_tokens=int(budget.get("max_total_tokens", 50000)),
            max_wall_seconds=int(budget.get("max_wall_seconds", 900)),
            max_parallel_tasks=int(budget.get("max_parallel_tasks", 3)),
        )

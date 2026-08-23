"""Persistent, user-editable task contracts for complex scheduled work.

TaskSpec is deliberately small: code enforces a canonical policy and structural
preflight; models may author or review only the editable task body.
"""

import copy
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.constants import PROJECT_ROOT


SCHEMA_VERSION = "1.0"

# System-owned values. A document embeds this snapshot for transparency, but
# execution always overlays this canonical copy.
BASE_POLICY = {
    "profile": "personal-safe-v1",
    "validator_model_tier": "high",
    "revalidate_after_edit": True,
    "unlisted_tools": "deny",
    "permission_escalation": "deny",
    "destructive_actions": "confirm",
    "external_publish": "confirm",
    "secrets_in_document": "deny",
    "network_default": "forbidden",
    "budget_overrun": "deny",
}

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*['\"]?[^\s,'\"]{8,}"
)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def policy_digest() -> str:
    return "sha256:" + hashlib.sha256(_canonical(BASE_POLICY).encode("utf-8")).hexdigest()


def content_digest(spec: dict) -> str:
    editable = {
        key: spec.get(key)
        for key in ("task", "execution", "output", "on_failure")
    }
    return "sha256:" + hashlib.sha256(_canonical(editable).encode("utf-8")).hexdigest()


def new_task_spec(goal: str, name: str = "", task_id: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    spec = {
        "contract": {
            "schema_version": SCHEMA_VERSION,
            "policy_profile": BASE_POLICY["profile"],
            "policy_digest": policy_digest(),
            "task_id": task_id or uuid.uuid4().hex[:12],
            "revision": 1,
            "original_goal": goal.strip(),
            "generated_by": "user",
            "generated_at": now,
        },
        "policy": copy.deepcopy(BASE_POLICY),
        "task": {
            "name": name.strip() or goal.strip()[:60] or "未命名任务",
            "objective": goal.strip(),
            "context": "",
            "assumptions": [],
            "required_inputs": {},
            "constraints": [],
            "acceptance_criteria": [],
        },
        "execution": {
            "complexity": "standard",
            "model_policy": {
                "recommended_tier": "low",
                "preferred_model": "",
                "allowed_models": [],
                "user_locked": False,
                "cost_advice": "inform",
            },
            "network": {
                "mode": "forbidden",
                "citation_required": False,
                "minimum_sources": 0,
            },
            "capabilities": [],
            "budget": {
                "max_total_tokens": 50000,
                "max_steps": 20,
                "max_wall_seconds": 900,
                "max_parallel_tasks": 3,
            },
            "plan": [],
            "approval": {
                "side_effects": "confirm",
                "confirmed": False,
            },
            "schedule": {
                "mode": "manual",
                "run_at": "",
                "cron": "",
                "timezone": "Asia/Shanghai",
            },
        },
        "output": {
            "format": "markdown",
            "language": "zh-CN",
            "full_delivery": "sqlite",
            "reply_mode": "summary",
            "max_chars": 0,
        },
        "on_failure": {
            "missing_condition": "ask_user",
            "tool_failure": "return_with_warnings",
            "model_failure": "fallback",
            "partial_result": "return_with_warnings",
            "max_retries": 1,
        },
    }
    spec["contract"]["content_digest"] = content_digest(spec)
    return spec


def normalize_task_spec(spec: dict) -> dict:
    """Overlay immutable policy values without silently repairing other fields."""
    normalized = copy.deepcopy(spec)
    normalized.setdefault("contract", {})
    normalized["contract"]["schema_version"] = SCHEMA_VERSION
    normalized["contract"]["policy_profile"] = BASE_POLICY["profile"]
    normalized["contract"]["policy_digest"] = policy_digest()
    normalized["policy"] = copy.deepcopy(BASE_POLICY)
    return normalized


def finding(code: str, message: str, path: str = "", resolution: str = "",
            severity: str = "blocker", overrideable: bool = False,
            source: str = "runtime") -> dict:
    return {
        "code": code,
        "message": message,
        "path": path,
        "resolution": resolution,
        "severity": severity,
        "overrideable": overrideable,
        "source": source,
    }


def preflight(spec: dict, config: Optional[dict] = None,
              available_capabilities: Optional[set] = None,
              available_tools: Optional[set] = None) -> dict:
    """Run deterministic checks. Model findings can never replace this result."""
    config = config or {}
    findings = []
    if not isinstance(spec, dict):
        return {"status": "blocked", "findings": [
            finding("UNSUPPORTED_SPEC", "TaskSpec 必须是 JSON object")
        ]}

    contract = spec.get("contract") or {}
    task = spec.get("task") or {}
    execution = spec.get("execution") or {}
    model_policy = execution.get("model_policy") or {}
    network = execution.get("network") or {}
    budget = execution.get("budget") or {}
    schedule = execution.get("schedule") or {}
    output = spec.get("output") or {}

    if contract.get("schema_version") != SCHEMA_VERSION:
        findings.append(finding(
            "UNSUPPORTED_SPEC", f"仅支持 TaskSpec {SCHEMA_VERSION}",
            "/contract/schema_version",
        ))
    if contract.get("policy_digest") != policy_digest() or spec.get("policy") != BASE_POLICY:
        findings.append(finding(
            "POLICY_MISMATCH", "不可变策略已被修改或版本不匹配",
            "/policy", "重新基于当前策略生成任务规则",
        ))
    if not str(task.get("objective") or "").strip():
        findings.append(finding(
            "MISSING_INPUT", "任务目标不能为空", "/task/objective",
        ))
    required_inputs = task.get("required_inputs", {})
    if not isinstance(required_inputs, dict):
        findings.append(finding(
            "MISSING_INPUT", "task.required_inputs 必须是 JSON object",
            "/task/required_inputs",
        ))
    else:
        for input_name, input_spec in required_inputs.items():
            if isinstance(input_spec, dict):
                required = input_spec.get("required", True)
                value = input_spec.get("value")
            else:
                required, value = True, input_spec
            if required and value in (None, "", [], {}):
                findings.append(finding(
                    "MISSING_INPUT", f"缺少必需输入 {input_name}",
                    f"/task/required_inputs/{input_name}",
                    "请填写 value，或将 required 设为 false",
                ))

    complexity = execution.get("complexity")
    if complexity not in {"simple", "standard", "complex"}:
        findings.append(finding(
            "INVALID_VALUE", "complexity 必须是 simple、standard 或 complex",
            "/execution/complexity",
        ))
    if network.get("mode") not in {"forbidden", "allowed", "required"}:
        findings.append(finding(
            "INVALID_VALUE", "network.mode 必须是 forbidden、allowed 或 required",
            "/execution/network/mode",
        ))
    if not isinstance(network.get("citation_required"), bool):
        findings.append(finding(
            "INVALID_VALUE", "network.citation_required 必须是 boolean",
            "/execution/network/citation_required",
        ))
    minimum_sources = network.get("minimum_sources")
    if not isinstance(minimum_sources, int) or not 0 <= minimum_sources <= 50:
        findings.append(finding(
            "INVALID_VALUE", "network.minimum_sources 必须是 0 到 50 的整数",
            "/execution/network/minimum_sources",
        ))
    elif network.get("citation_required") and minimum_sources < 1:
        findings.append(finding(
            "INVALID_VALUE", "要求引用时 minimum_sources 至少为 1",
            "/execution/network/minimum_sources",
        ))
    if network.get("mode") == "forbidden" and (
        network.get("citation_required") or (isinstance(minimum_sources, int) and minimum_sources > 0)
    ):
        findings.append(finding(
            "INVALID_VALUE", "禁止联网与引用/来源数量要求冲突",
            "/execution/network",
        ))
    if model_policy.get("recommended_tier") not in {"low", "standard", "high"}:
        findings.append(finding(
            "INVALID_VALUE", "recommended_tier 必须是 low、standard 或 high",
            "/execution/model_policy/recommended_tier",
        ))
    if model_policy.get("cost_advice") not in {"off", "inform", "confirm"}:
        findings.append(finding(
            "INVALID_VALUE", "cost_advice 必须是 off、inform 或 confirm",
            "/execution/model_policy/cost_advice",
        ))

    models = (config.get("llm") or {}).get("models") or {}
    preferred = model_policy.get("preferred_model") or ""
    if preferred and preferred not in models:
        findings.append(finding(
            "MODEL_UNAVAILABLE", f"模型 {preferred!r} 未配置",
            "/execution/model_policy/preferred_model",
        ))
    allowed_models = model_policy.get("allowed_models") or []
    for model_name in allowed_models:
        if model_name not in models:
            findings.append(finding(
                "MODEL_UNAVAILABLE", f"允许模型 {model_name!r} 未配置",
                "/execution/model_policy/allowed_models",
            ))
    if preferred and allowed_models and preferred not in allowed_models:
        findings.append(finding(
            "MODEL_UNAVAILABLE", "preferred_model 不在 allowed_models 中",
            "/execution/model_policy/preferred_model",
        ))
    if model_policy.get("user_locked") and not preferred:
        findings.append(finding(
            "MODEL_UNAVAILABLE", "user_locked=true 时必须指定 preferred_model",
            "/execution/model_policy/preferred_model",
        ))

    limits = {
        "max_total_tokens": (1, 2_000_000),
        "max_steps": (1, 1000),
        "max_wall_seconds": (1, 86400),
        "max_parallel_tasks": (1, 16),
    }
    for key, (minimum, maximum) in limits.items():
        value = budget.get(key)
        if not isinstance(value, int) or not minimum <= value <= maximum:
            findings.append(finding(
                "BUDGET_INVALID",
                f"{key} 必须在 {minimum} 到 {maximum} 之间",
                f"/execution/budget/{key}",
            ))

    required_caps = []
    for item in execution.get("capabilities") or []:
        name = item if isinstance(item, str) else item.get("name", "")
        required = True if isinstance(item, str) else item.get("required", True)
        if name and required:
            required_caps.append(name)
    if available_capabilities is not None:
        for name in required_caps:
            if name not in available_capabilities:
                findings.append(finding(
                    "CAPABILITY_MISSING", f"缺少能力 {name}",
                    "/execution/capabilities",
                ))

    plan = execution.get("plan") or []
    if not isinstance(plan, list):
        findings.append(finding(
            "INVALID_PLAN", "execution.plan 必须是数组", "/execution/plan",
        ))
        plan = []
    node_ids = [str(node.get("id") or "") for node in plan if isinstance(node, dict)]
    if any(not node_id for node_id in node_ids) or len(node_ids) != len(set(node_ids)):
        findings.append(finding(
            "INVALID_PLAN", "每个计划节点必须有唯一且非空的 id", "/execution/plan",
        ))
    known_ids = set(node_ids)
    dependencies = {}
    for index, node in enumerate(plan):
        if not isinstance(node, dict):
            findings.append(finding(
                "INVALID_PLAN", "计划节点必须是 JSON object", f"/execution/plan/{index}",
            ))
            continue
        node_id = str(node.get("id") or "")
        deps = list(node.get("depends_on") or [])
        dependencies[node_id] = deps
        unknown = [dep for dep in deps if dep not in known_ids]
        if unknown:
            findings.append(finding(
                "INVALID_PLAN", f"节点 {node_id} 引用了不存在的依赖 {unknown}",
                f"/execution/plan/{index}/depends_on",
            ))
        mode_value = node.get("mode", "agent")
        if mode_value not in {"tool", "agent"}:
            findings.append(finding(
                "INVALID_PLAN", "节点 mode 必须是 tool 或 agent",
                f"/execution/plan/{index}/mode",
            ))
        node_model = ((node.get("executor") or {}).get("preferred_model") or "")
        if node_model and node_model not in models:
            findings.append(finding(
                "MODEL_UNAVAILABLE", f"节点模型 {node_model!r} 未配置",
                f"/execution/plan/{index}/executor/preferred_model",
            ))
        if mode_value == "tool":
            tool = node.get("tool") or {}
            tool_name = tool.get("name") or ""
            if not tool_name or not isinstance(tool.get("arguments", {}), dict):
                findings.append(finding(
                    "INVALID_PLAN", "tool 节点必须提供工具名和 JSON object arguments",
                    f"/execution/plan/{index}/tool",
                ))
            elif available_tools is not None and tool_name not in available_tools:
                findings.append(finding(
                    "CAPABILITY_MISSING", f"工具 {tool_name!r} 未注册",
                    f"/execution/plan/{index}/tool/name",
                ))

    visiting, visited = set(), set()
    def _visit(node_id):
        if node_id in visiting:
            return False
        if node_id in visited:
            return True
        visiting.add(node_id)
        for dep in dependencies.get(node_id, []):
            if dep in known_ids and not _visit(dep):
                return False
        visiting.remove(node_id)
        visited.add(node_id)
        return True
    if any(not _visit(node_id) for node_id in known_ids):
        findings.append(finding(
            "INVALID_PLAN", "计划中存在循环依赖", "/execution/plan",
        ))

    mode = schedule.get("mode")
    if mode not in {"manual", "once", "repeat"}:
        findings.append(finding(
            "INVALID_SCHEDULE", "schedule.mode 必须是 manual、once 或 repeat",
            "/execution/schedule/mode",
        ))
    elif mode == "once":
        try:
            _parse_datetime(schedule.get("run_at", ""))
        except ValueError as exc:
            findings.append(finding("INVALID_SCHEDULE", str(exc), "/execution/schedule/run_at"))
    elif mode == "repeat" and not _valid_cron(schedule.get("cron", "")):
        findings.append(finding(
            "INVALID_SCHEDULE", "重复任务仅支持 HH:MM 或 */N * * * *",
            "/execution/schedule/cron",
        ))

    if _SECRET_RE.search(_canonical(spec)):
        findings.append(finding(
            "SECRET_IN_DOCUMENT", "TaskSpec 中疑似包含密钥或密码",
            resolution="改为引用服务器已配置的凭据名称，不要写入实际值",
        ))

    if output.get("full_delivery") not in {
        "auto", "email", "hedgedoc", "sqlite", "inline"
    }:
        findings.append(finding(
            "INVALID_OUTPUT",
            "output.full_delivery 必须是 auto、email、hedgedoc、sqlite 或 inline",
            "/output/full_delivery",
        ))
    if output.get("reply_mode") not in {"summary", "preview"}:
        findings.append(finding(
            "INVALID_OUTPUT", "output.reply_mode 必须是 summary 或 preview",
            "/output/reply_mode",
        ))
    approval = execution.get("approval") or {}
    if output.get("full_delivery") in {"auto", "hedgedoc"} and not approval.get("confirmed"):
        findings.append(finding(
            "EXTERNAL_PUBLISH_CONFIRM",
            "HedgeDoc 是公开链接，选择 auto 或 hedgedoc 前必须显式确认",
            "/execution/approval/confirmed",
            "确认公开投递后将 execution.approval.confirmed 设为 true",
        ))
    max_chars = output.get("max_chars", 0)
    if not isinstance(max_chars, int) or not 0 <= max_chars <= 1_000_000:
        findings.append(finding(
            "INVALID_OUTPUT", "output.max_chars 必须是 0 到 1000000 的整数",
            "/output/max_chars",
        ))

    blockers = [f for f in findings if f["severity"] == "blocker"]
    return {
        "status": "blocked" if blockers else "ready",
        "task_id": contract.get("task_id", ""),
        "revision": contract.get("revision", 0),
        "content_digest": content_digest(spec),
        "findings": findings,
    }


def _parse_datetime(value: str) -> datetime:
    if not value:
        raise ValueError("一次性任务必须提供 run_at")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("run_at 必须是 ISO8601 时间")
    if dt.tzinfo is None:
        raise ValueError("run_at 必须包含时区")
    return dt.astimezone(timezone.utc)


def _valid_cron(value: str) -> bool:
    if re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value or ""):
        return True
    match = re.fullmatch(r"\*/(\d+) \* \* \* \*", value or "")
    return bool(match and 1 <= int(match.group(1)) <= 1440)


def next_run_at(spec: dict, after: Optional[datetime] = None) -> Optional[str]:
    schedule = ((spec.get("execution") or {}).get("schedule") or {})
    mode = schedule.get("mode", "manual")
    if mode == "manual":
        return None
    if mode == "once":
        return _parse_datetime(schedule.get("run_at", "")).isoformat()

    now = (after or datetime.now(timezone.utc)).astimezone()
    cron = schedule.get("cron", "")
    if ":" in cron:
        hour, minute = (int(x) for x in cron.split(":"))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc).isoformat()
    interval = int(cron.split("/")[1].split()[0])
    candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while candidate.minute % interval != 0:
        candidate += timedelta(minutes=1)
    return candidate.astimezone(timezone.utc).isoformat()


class TaskSpecStore:
    def __init__(self, db_path: str = ""):
        self.db_path = db_path or os.path.join(PROJECT_ROOT, "data", "task_specs.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_specs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    last_run_status TEXT,
                    last_run_result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    def save(self, spec: dict, status: str = "draft", enabled: bool = False):
        normalized = normalize_task_spec(spec)
        task_id = normalized["contract"].get("task_id") or uuid.uuid4().hex[:12]
        normalized["contract"]["task_id"] = task_id
        normalized["contract"]["content_digest"] = content_digest(normalized)
        name = (normalized.get("task") or {}).get("name") or task_id
        now = datetime.now(timezone.utc).isoformat()
        run_at = next_run_at(normalized) if enabled else None
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO task_specs
                    (id,name,spec_json,status,enabled,next_run_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, spec_json=excluded.spec_json,
                    status=excluded.status, enabled=excluded.enabled,
                    next_run_at=excluded.next_run_at, updated_at=excluded.updated_at
            """, (task_id, name, _canonical(normalized), status, int(enabled),
                  run_at, now, now))
        return self.get(task_id)

    def get(self, task_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_specs WHERE id=?", (task_id,)).fetchone()
        return self._row(row) if row else None

    def list(self) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_specs ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def delete(self, task_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM task_specs WHERE id=?", (task_id,))
            return cur.rowcount > 0

    def due(self, now: Optional[datetime] = None) -> list:
        now_iso = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM task_specs
                WHERE enabled=1 AND status='approved'
                  AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at
            """, (now_iso,)).fetchall()
        return [self._row(row) for row in rows]

    def mark_started(self, task_id: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT spec_json FROM task_specs WHERE id=?", (task_id,)).fetchone()
            if not row:
                return
            spec = json.loads(row["spec_json"])
            schedule = spec["execution"]["schedule"]
            if schedule.get("mode") == "once":
                enabled, following = 0, None
            else:
                enabled, following = 1, next_run_at(spec, datetime.now(timezone.utc))
            conn.execute("""
                UPDATE task_specs SET enabled=?, next_run_at=?, last_run_at=?,
                    last_run_status='running', updated_at=? WHERE id=?
            """, (enabled, following, now, now, task_id))

    def mark_finished(self, task_id: str, ok: bool, result: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute("""
                UPDATE task_specs SET last_run_status=?, last_run_result=?,
                    updated_at=? WHERE id=?
            """, ("succeeded" if ok else "failed", str(result)[:20000], now, task_id))

    @staticmethod
    def _row(row) -> dict:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["spec"] = json.loads(item.pop("spec_json"))
        return item

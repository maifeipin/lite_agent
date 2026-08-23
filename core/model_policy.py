"""Request-scoped model selection shared by chat and DAG execution."""

from dataclasses import dataclass
from enum import Enum
import re
from typing import Optional


class ModelPolicyError(ValueError):
    """A requested model cannot satisfy the configured execution policy."""


class ModelLock(str, Enum):
    AUTO = "auto"
    PREFERRED = "preferred"
    HARD = "hard"


@dataclass(frozen=True)
class ExecutionPolicy:
    requested_model: str = ""
    model_lock: ModelLock = ModelLock.AUTO
    lock_source: str = "system"
    allowed_models: tuple[str, ...] = ()
    max_steps: Optional[int] = None
    max_total_tokens: Optional[int] = None
    max_wall_seconds: Optional[int] = None
    max_parallel_tasks: Optional[int] = None

    def __post_init__(self):
        if not isinstance(self.model_lock, ModelLock):
            object.__setattr__(self, "model_lock", ModelLock(self.model_lock))
        if not isinstance(self.allowed_models, tuple):
            object.__setattr__(self, "allowed_models", tuple(self.allowed_models or ()))

    @classmethod
    def user_locked(cls, model: str, **kwargs) -> "ExecutionPolicy":
        return cls(
            requested_model=model,
            model_lock=ModelLock.HARD,
            lock_source="user",
            **kwargs,
        )


@dataclass(frozen=True)
class ModelDecision:
    model: str
    role: str
    reason: str
    fallback_models: tuple[str, ...] = ()


def normalize_model_alias(value: str) -> str:
    """Normalize human aliases without guessing provider or model families."""
    return re.sub(r"[\s_-]+", "-", str(value or "").strip().lower())


class ModelSelector:
    """Resolve one configured model using explicit, auditable precedence."""

    def __init__(self, config: dict):
        self.config = config
        self.llm_cfg = config.get("llm", {}) or {}
        self.models = self.llm_cfg.get("models", {}) or {}
        self.routing = config.get("task_routing", {}) or {}
        self._aliases = self._build_aliases()

    def _build_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for name, cfg in self.models.items():
            values = [name] + list((cfg or {}).get("aliases") or [])
            for value in values:
                normalized = normalize_model_alias(value)
                existing = aliases.get(normalized)
                if existing and existing != name:
                    raise ModelPolicyError(
                        f"模型别名 {value!r} 同时指向 {existing!r} 和 {name!r}"
                    )
                aliases[normalized] = name
        return aliases

    def resolve_name(self, value: str, required: bool = False) -> str:
        name = self._aliases.get(normalize_model_alias(value), "")
        if required and not name:
            raise ModelPolicyError(f"未配置模型: {value}")
        return name

    def extract_override(self, text: str) -> str:
        """Extract an explicitly requested configured model from user text."""
        bracket = re.search(r"\[model=([^\]]+)\]", text or "", re.IGNORECASE)
        if bracket:
            return self.resolve_name(bracket.group(1).strip(), required=True)

        candidates = []
        for name, cfg in self.models.items():
            for value in [name] + list((cfg or {}).get("aliases") or []):
                candidates.append((str(value), name))
        lowered = str(text or "").lower()
        suffix = r"(?=\s|来|去|查|搜|找|做|分析|整理|清理|总结|，|,|$)"
        for value, name in sorted(candidates, key=lambda item: len(item[0]), reverse=True):
            alias = re.escape(value.lower())
            alias = re.sub(r"(?:\\[ _-]|[ _-])+", r"[\\s_-]+", alias)
            if re.search(rf"(?:用|使用|指定)\s*{alias}{suffix}", lowered):
                return name
        return ""

    def _route_rule(self, subtask_type: str) -> dict:
        for rule in self.routing.get("route_rules") or []:
            if isinstance(rule, dict) and rule.get("type") == subtask_type:
                return rule
        return {}

    def route_tools(self, subtask_type: str) -> list[str]:
        return list(self._route_rule(subtask_type).get("tools") or [])

    @staticmethod
    def _allowed(name: str, allowed: Optional[set[str]]) -> bool:
        return bool(name and (allowed is None or name in allowed))

    def select(self, role: str, *, subtask_type: str = "",
               policy: ExecutionPolicy = None,
               preferred_model: str = "") -> ModelDecision:
        policy = policy or ExecutionPolicy()
        rule = self._route_rule(subtask_type) if role == "worker" else {}

        policy_allowed = list(policy.allowed_models) or None
        route_allowed = rule.get("allowed_models")
        route_allowed = route_allowed if isinstance(route_allowed, list) else None
        if policy_allowed is not None and route_allowed is not None:
            allowed_names = [
                name for name in policy_allowed if name in set(route_allowed)
            ]
        elif policy_allowed is not None:
            allowed_names = policy_allowed
        elif route_allowed is not None:
            allowed_names = route_allowed
        else:
            allowed_names = None
        allowed = set(allowed_names) if allowed_names is not None else None

        requested = ""
        if policy.requested_model:
            requested = self.resolve_name(policy.requested_model, required=True)
            if not self._allowed(requested, allowed):
                if policy.model_lock == ModelLock.HARD:
                    raise ModelPolicyError(
                        f"{subtask_type or role} 不允许模型 {requested!r}"
                    )
                requested = ""
        if requested and policy.model_lock == ModelLock.HARD:
            return ModelDecision(
                model=requested,
                role=role,
                reason=f"{policy.lock_source}:hard",
                fallback_models=(),
            )

        preferred = self.resolve_name(preferred_model) if preferred_model else ""
        if self._allowed(preferred, allowed):
            return ModelDecision(
                model=preferred,
                role=role,
                reason="node:preferred",
                fallback_models=self._fallbacks(rule, preferred, allowed),
            )

        if requested and policy.model_lock == ModelLock.PREFERRED:
            return ModelDecision(
                model=requested,
                role=role,
                reason=f"{policy.lock_source}:preferred",
                fallback_models=self._fallbacks(rule, requested, allowed),
            )

        if role == "planner":
            selected = self.routing.get("planner_model")
            reason = "role:planner"
        elif role == "aggregator":
            selected = (
                self.routing.get("aggregator_model")
                or self.routing.get("planner_model")
            )
            reason = "role:aggregator"
        elif role == "worker" and rule:
            selected = rule.get("model")
            reason = f"route:{subtask_type}"
        else:
            selected = self.llm_cfg.get("default")
            reason = "llm:default"

        if not self._allowed(selected, allowed):
            selected = next(
                (name for name in (allowed_names or ()) if name in self.models), ""
            )
            reason = "allowed:first"
        if not selected or selected not in self.models:
            raise ModelPolicyError(f"{role} 没有可用的已配置模型")
        return ModelDecision(
            model=selected,
            role=role,
            reason=reason,
            fallback_models=self._fallbacks(rule, selected, allowed),
        )

    def _fallbacks(self, rule: dict, selected: str,
                   allowed: Optional[set[str]]) -> tuple[str, ...]:
        fallback = str((rule or {}).get("fallback") or "")
        if (
            fallback
            and fallback != selected
            and fallback in self.models
            and self._allowed(fallback, allowed)
        ):
            return (fallback,)
        return ()

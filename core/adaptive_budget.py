"""Shared adaptive step budget for direct chat loops and DAG workers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class BudgetAction(str, Enum):
    EXECUTE = "execute"
    EXTEND = "extend"
    REPLAN = "replan"
    FINALIZE = "finalize"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class AdaptiveBudgetPolicy:
    enabled: bool = False
    simple_hard_steps: int = 60
    initial_steps: int = 8
    lease_steps: int = 6
    finalization_reserve: int = 1
    low_yield_streak: int = 3
    low_result_chars: int = 80
    max_context_chars: int = 24000
    recent_tool_rounds: int = 2

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "AdaptiveBudgetPolicy":
        raw = dict((config or {}).get("adaptive_execution") or {})
        return cls(
            enabled=bool(raw.get("enabled", False)),
            simple_hard_steps=max(3, int(raw.get("simple_hard_steps", 60))),
            initial_steps=max(1, int(raw.get("initial_steps", 8))),
            lease_steps=max(1, int(raw.get("lease_steps", 6))),
            finalization_reserve=max(1, int(raw.get("finalization_reserve", 1))),
            low_yield_streak=max(2, int(raw.get("low_yield_streak", 3))),
            low_result_chars=max(0, int(raw.get("low_result_chars", 80))),
            max_context_chars=max(4000, int(raw.get("max_context_chars", 24000))),
            recent_tool_rounds=max(1, int(raw.get("recent_tool_rounds", 2))),
        )


@dataclass(frozen=True)
class ProgressSignal:
    step: int
    tool_name: str
    gain: str
    reason: str
    output_chars: int


@dataclass(frozen=True)
class BudgetDecision:
    action: BudgetAction
    reason: str
    lease_limit: int
    instruction: str = ""


class EvidenceBoard:
    """Bounded evidence view; raw tool output is not repeated in model context."""

    WEB_TOOLS = frozenset({"web_search", "ops_web_fetch", "web_clip"})

    def __init__(self, low_result_chars: int = 80):
        self.low_result_chars = max(0, int(low_result_chars))
        self.items: list[dict] = []
        self.signals: list[ProgressSignal] = []
        self.low_yield_streak = 0
        self._calls: set[str] = set()
        self._outputs: set[str] = set()

    @staticmethod
    def _canonical_url(value: str) -> str:
        try:
            parts = urlsplit(value)
            if not parts.scheme or not parts.netloc:
                return value
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                     if not k.lower().startswith("utm_")]
            return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                               parts.path.rstrip("/"), urlencode(query), ""))
        except Exception:
            return value

    @classmethod
    def _canonical_arguments(cls, arguments: str) -> str:
        try:
            parsed = json.loads(arguments or "{}")
            if isinstance(parsed, dict):
                for key in ("url", "urls"):
                    if isinstance(parsed.get(key), str):
                        parsed[key] = cls._canonical_url(parsed[key])
                return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        except Exception:
            pass
        return str(arguments or "").strip()

    def record(self, *, step: int, tool_name: str, arguments: str,
               ok: bool, output: str) -> ProgressSignal:
        text = str(output or "").strip()
        args = self._canonical_arguments(arguments)
        call_fp = hashlib.sha256(f"{tool_name}:{args}".encode()).hexdigest()
        output_fp = hashlib.sha256(text.encode()).hexdigest() if text else ""
        duplicate = call_fp in self._calls or bool(output_fp and output_fp in self._outputs)
        self._calls.add(call_fp)
        if output_fp:
            self._outputs.add(output_fp)

        if not ok:
            gain, reason = "low", "tool_failed"
        elif not text:
            gain, reason = "low", "empty_result"
        elif duplicate:
            gain, reason = "low", "duplicate_result"
        elif tool_name in self.WEB_TOOLS and len(text) < self.low_result_chars:
            gain, reason = "low", "short_web_result"
        elif tool_name not in self.WEB_TOOLS or len(text) >= 500:
            gain, reason = "high", "new_evidence"
        else:
            gain, reason = "medium", "partial_evidence"

        self.low_yield_streak = self.low_yield_streak + 1 if gain == "low" else 0
        signal = ProgressSignal(step, tool_name, gain, reason, len(text))
        self.signals = (self.signals + [signal])[-12:]
        if text and not duplicate:
            self.items = (self.items + [{
                "step": step, "tool": tool_name, "args": args[:500],
                "output": text[:1200] + ("…" if len(text) > 1200 else ""),
                "gain": gain,
            }])[-16:]
        return signal

    @property
    def last_gain(self) -> str:
        return self.signals[-1].gain if self.signals else "unknown"

    def render(self, max_chars: int = 10000) -> str:
        lines = ["【执行证据看板｜原始结果不重复注入上下文】"]
        for item in self.items:
            lines.append(
                f"- step {item['step']} [{item['gain']}] {item['tool']} "
                f"args={item['args']}\n  {item['output']}"
            )
        return "\n".join(lines)[-max_chars:]

    def snapshot(self) -> dict:
        return {
            "low_yield_streak": self.low_yield_streak,
            "last_gain": self.last_gain,
            "recent_signals": [item.__dict__ for item in self.signals[-6:]],
            "evidence_items": len(self.items),
        }


class AdaptiveStepController:
    """Grants short leases inside an immutable hard limit and reserves summary."""

    def __init__(self, hard_steps: int, policy: AdaptiveBudgetPolicy, goal: str = ""):
        self.hard_steps = max(1, int(hard_steps))
        self.policy = policy
        self.enabled = bool(policy.enabled and self.hard_steps >= 3)
        reserve = min(policy.finalization_reserve, self.hard_steps - 1)
        self.execution_cap = self.hard_steps - reserve
        self.lease_limit = (min(self.execution_cap, policy.initial_steps)
                            if self.enabled else self.hard_steps)
        self.goal = str(goal or "")
        self.board = EvidenceBoard(policy.low_result_chars)
        self.replan_used = False
        self.finalizing = False

    def record_tool_result(self, **kwargs) -> ProgressSignal:
        return self.board.record(**kwargs)

    def _result(self, action: BudgetAction, reason: str,
                instruction: str = "") -> BudgetDecision:
        return BudgetDecision(action, reason, self.lease_limit, instruction)

    def before_step(self, next_step: int,
                    reviewer: Optional[Callable[[dict], Any]] = None) -> BudgetDecision:
        if not self.enabled:
            action = BudgetAction.EXECUTE if next_step <= self.hard_steps else BudgetAction.EXHAUSTED
            return self._result(action, "fixed_budget")
        if self.finalizing:
            action = BudgetAction.FINALIZE if next_step <= self.hard_steps else BudgetAction.EXHAUSTED
            return self._result(action, "finalization_reserved")

        if self.board.low_yield_streak >= self.policy.low_yield_streak:
            if not self.replan_used and next_step <= self.execution_cap:
                self.replan_used = True
                self.board.low_yield_streak = 0
                self.lease_limit = min(
                    self.execution_cap,
                    max(self.lease_limit, next_step + self.policy.lease_steps - 1),
                )
                return self._result(
                    BudgetAction.REPLAN, "low_yield_replan",
                    "最近多次工具调用没有增加有效证据。停止重复当前路线，"
                    "换用结构化数据、权威来源或其他工具；没有新路线就立即总结。",
                )
            if next_step > self.lease_limit:
                self.finalizing = True
                return self._result(
                    BudgetAction.FINALIZE, "low_yield_after_replan",
                    self.finalization_instruction(),
                )
        if next_step <= self.lease_limit:
            return self._result(BudgetAction.EXECUTE, "within_lease")
        if next_step > self.execution_cap:
            self.finalizing = True
            return self._result(BudgetAction.FINALIZE, "execution_cap_reached",
                                self.finalization_instruction())
        if self.board.last_gain == "high":
            return self._extend("high_gain", self.policy.lease_steps)

        reviewed = self._review(reviewer, next_step) if reviewer else None
        if reviewed:
            return reviewed
        return self._extend("bounded_ambiguity", min(2, self.policy.lease_steps))

    def _extend(self, reason: str, steps: int) -> BudgetDecision:
        old = self.lease_limit
        self.lease_limit = min(self.execution_cap, old + max(1, steps))
        if self.lease_limit > old:
            return self._result(BudgetAction.EXTEND,
                                f"{reason}:{old}->{self.lease_limit}")
        self.finalizing = True
        return self._result(BudgetAction.FINALIZE, "no_execution_budget",
                            self.finalization_instruction())

    def _review(self, reviewer: Callable[[dict], Any],
                next_step: int) -> Optional[BudgetDecision]:
        try:
            value = reviewer({
                "goal": self.goal, "next_step": next_step,
                "hard_steps": self.hard_steps, "lease_limit": self.lease_limit,
                "execution_cap": self.execution_cap, "progress": self.board.snapshot(),
            }) or {}
            action = str(value.get("action") or "").lower()
            if action == "extend":
                grant = min(max(1, int(value.get("grant_steps") or 1)),
                            self.policy.lease_steps)
                return self._extend(str(value.get("reason") or "supervisor_extend"), grant)
            if action == "replan":
                self.replan_used = True
                self.lease_limit = min(self.execution_cap, self.lease_limit + 2)
                return self._result(
                    BudgetAction.REPLAN,
                    str(value.get("reason") or "supervisor_replan"),
                    str(value.get("instruction") or "请更换执行路线。"),
                )
            if action == "finalize":
                self.finalizing = True
                return self._result(
                    BudgetAction.FINALIZE,
                    str(value.get("reason") or "supervisor_finalize"),
                    self.finalization_instruction(),
                )
        except Exception:
            pass  # Reviewer is advisory; deterministic finalization remains safe.
        return None

    def finalization_instruction(self) -> str:
        return (
            "进入最终汇总阶段：不得再调用任何工具。请仅根据已有证据给出"
            "完整的最佳答案；区分已确认事实、合理推断和未验证缺口，"
            f"不要编造，也不要只回复预算已耗尽。\n原始目标：{self.goal[:2000]}"
        )


def compact_messages(messages: list[dict], *, base_message_count: int,
                     board: EvidenceBoard, max_chars: int,
                     recent_tool_rounds: int) -> list[dict]:
    """Return a bounded provider view without mutating persisted history."""
    size = lambda item: len(str(item.get("content", "")))
    if sum(map(size, messages)) <= max_chars:
        return messages

    base, current = messages[:base_message_count], messages[base_message_count:]
    systems = [dict(item) for item in base if item.get("role") == "system"]
    conversation = [dict(item) for item in base
                    if item.get("role") not in ("system", "tool")
                    and not item.get("tool_calls")][-6:]
    for item in conversation:
        if isinstance(item.get("content"), str) and len(item["content"]) > 4000:
            item["content"] = item["content"][-4000:]

    starts = [i for i, item in enumerate(current)
              if item.get("role") == "assistant" and item.get("tool_calls")]
    start = starts[max(0, len(starts) - recent_tool_rounds)] if starts else max(0, len(current) - 4)
    recent = [dict(item) for item in current[start:]]
    item_limit = max(800, min(4000, max_chars // max(4, len(recent))))
    for item in recent:
        if isinstance(item.get("content"), str) and len(item["content"]) > item_limit:
            item["content"] = item["content"][:item_limit] + "\n…[原始内容未重复注入]"

    fixed = sum(map(size, systems + conversation + recent))
    board_message = {"role": "user", "content": board.render(
        max(1200, min(10000, max_chars - fixed))
    )}
    return systems + conversation + [board_message] + recent

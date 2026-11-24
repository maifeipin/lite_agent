"""
CLI 引擎统一适配器抽象基类 (daemon.adapters.base).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

MAX_OUTPUT_CHARS = 65536
VALID_EXECUTION_MODES = ("agent", "plan", "chat")
VALID_REASONING_EFFORTS = ("", "low", "medium", "high", "xhigh")

EventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]
ApprovalCallback = Callable[[Dict[str, Any]], Awaitable[str]]


def normalize_execution_mode(mode: Optional[str]) -> str:
    cleaned = str(mode or "agent").strip().lower()
    return cleaned if cleaned in VALID_EXECUTION_MODES else "agent"


def normalize_reasoning_effort(effort: Optional[str]) -> str:
    cleaned = str(effort or "").strip().lower()
    return cleaned if cleaned in VALID_REASONING_EFFORTS else ""


def apply_execution_mode_prompt(
    prompt: str,
    execution_mode: Optional[str] = "agent",
    reasoning_effort: Optional[str] = "",
    *,
    include_effort_hint: bool = False,
) -> str:
    """统一工作模式与思考深度的轻量提示词前缀抽象（跨引擎复用）。"""
    mode = normalize_execution_mode(execution_mode)
    effort = normalize_reasoning_effort(reasoning_effort)
    prefixes: list[str] = []
    if mode == "plan":
        prefixes.append("[模式: PLAN - 仅进行只读分析并给出清晰实施计划，不要修改文件或执行写入/副作用命令]")
    elif mode == "chat":
        prefixes.append("[模式: CHAT - 纯问答与讨论模式，不要修改文件或执行有副作用的命令]")
    if include_effort_hint and effort:
        prefixes.append(f"[思考深度: {effort} - 请按该深度进行推理与分析]")
    if not prefixes:
        return prompt
    return "\n".join(prefixes) + "\n\n" + prompt


def truncate_text(value: Any, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = value if isinstance(value, str) else str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


@dataclass
class TurnResult:
    status: str  # "completed" | "cancelled" | "failed"
    native_session_id: Optional[str] = None
    final_text: str = ""
    error: Optional[str] = None


class BaseCliAdapter(ABC):
    """Codex / AGY 等 CLI 引擎的统一异步执行接口。"""

    @abstractmethod
    async def run_turn(
        self,
        *,
        bot_id: str,
        turn_id: str,
        cwd: str,
        prompt: str,
        native_session_id: Optional[str],
        approval_mode: str,
        model: Optional[str],
        on_event: EventCallback,
        on_approval: Optional[ApprovalCallback] = None,
        execution_mode: str = "agent",
        reasoning_effort: str = "",
    ) -> TurnResult:
        """执行单个用户指令回合，实时回调事件流并返回最终状态。"""

    @abstractmethod
    async def cancel_turn(self, bot_id: str, turn_id: str) -> None:
        """中止指定回合的底层 CLI 子进程树。"""

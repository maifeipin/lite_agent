"""
CLI 引擎统一适配器抽象基类 (daemon.adapters.base).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

MAX_OUTPUT_CHARS = 65536

EventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]
ApprovalCallback = Callable[[Dict[str, Any]], Awaitable[str]]


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
    ) -> TurnResult:
        """执行单个用户指令回合，实时回调事件流并返回最终状态。"""

    @abstractmethod
    async def cancel_turn(self, bot_id: str, turn_id: str) -> None:
        """中止指定回合的底层 CLI 子进程树。"""

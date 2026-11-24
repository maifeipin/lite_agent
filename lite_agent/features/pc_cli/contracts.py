"""
PC CLI 手机管理中枢契约与常量定义 (features.pc_cli.contracts).
纯标准库定义，供中枢插件内各模块共享。
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Optional, Tuple


class NodeStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class BotStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    ERROR = "error"


class TurnStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ApprovalType(str, Enum):
    CLI_TOOL = "cli_tool"
    BOT_DELETE = "bot_delete"
    TURN_CANCEL = "turn_cancel"


class ApprovalDecision(str, Enum):
    PENDING = "pending"
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"
    EXPIRED = "expired"


class EventType(str, Enum):
    USER_MESSAGE = "user_message"
    THINKING = "thinking"
    TOOL_START = "tool_start"
    TOOL_UPDATE = "tool_update"
    TOOL_END = "tool_end"
    TEXT_DELTA = "text_delta"
    ASSISTANT_MESSAGE = "assistant_message"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESOLVED = "approval_resolved"
    TURN_STATUS = "turn_status"
    ERROR = "error"


SUPPORTED_ENGINES: Tuple[str, ...] = ("codex", "agy")
APPROVAL_MODES: Tuple[str, ...] = ("auto", "ask")
EXECUTION_MODES: Tuple[str, ...] = ("agent", "plan", "chat")
REASONING_EFFORTS: Tuple[str, ...] = ("", "low", "medium", "high", "xhigh")
VALID_DECISIONS: Tuple[str, ...] = (
    ApprovalDecision.ALLOW_ONCE.value,
    ApprovalDecision.ALLOW_SESSION.value,
    ApprovalDecision.DENY.value,
)
MAX_EVENT_TEXT_CHARS = 65536
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


def is_valid_model_name(model: Optional[str]) -> bool:
    """校验可选模型名，严禁空白或 shell/cmd 元字符。"""
    if model is None or model == "":
        return True
    return bool(MODEL_NAME_RE.match(model))


def now_ms() -> int:
    """返回当前 UTC 毫秒时间戳。"""
    return int(time.time() * 1000)

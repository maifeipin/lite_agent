"""
Lite Agent 核心契约与标准化协议类型 (Contracts)。

本包定义系统核心共享的数据类型、执行上下文与结果协议。
所有定义仅依赖 Python 标准库，绝对无副作用，不依赖 Agent 实现、配置加载、SDK、数据库或具体业务。
"""

from contracts.message import IncomingMessage, AgentResponse
from contracts.result import TaskResult
from contracts.execution import (
    ActorType,
    ExecutionSource,
    ExecutionContext,
    ExecutionResult,
    SkillPolicy,
    _deep_freeze,
)

__all__ = [
    "IncomingMessage",
    "AgentResponse",
    "TaskResult",
    "ActorType",
    "ExecutionSource",
    "ExecutionContext",
    "ExecutionResult",
    "SkillPolicy",
    "_deep_freeze",
]

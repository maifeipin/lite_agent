"""
Phase 1a — 执行上下文与结果协议 (向后兼容层)。

本模块将原核心执行协议重定向至 contracts.execution。
确保所有从 core.execution 导入的类对象与 contracts.execution 完全是同一个对象 (is 恒成立)。
"""

from lite_agent.contracts.execution import (
    ActorType,
    ExecutionSource,
    _deep_freeze,
    ExecutionContext,
    ExecutionResult,
    SkillPolicy,
)

__all__ = [
    "ActorType",
    "ExecutionSource",
    "_deep_freeze",
    "ExecutionContext",
    "ExecutionResult",
    "SkillPolicy",
]
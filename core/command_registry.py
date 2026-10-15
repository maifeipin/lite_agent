"""
指令注册表 — 统一 slash 指令的路由分发 (向后兼容层)。
内部委托给 services.commands.CommandRegistry，保持单例与现有 API 行为完全不变。
"""

from services.commands import (
    CommandRegistry,
    default_command_registry as _registry,
    slash_command,
    dispatch,
)

__all__ = [
    "CommandRegistry",
    "_registry",
    "slash_command",
    "dispatch",
]

"""
Lite Agent 通用平台服务层 (Services)。

提供指令、输出交付、调度通知与应用通用机制。
依赖 contracts 与通用工具库，绝对不反向依赖 features 或 skills 业务逻辑。
"""

from services.commands import (
    CommandRegistry,
    default_command_registry,
    slash_command,
    dispatch,
)

__all__ = [
    "CommandRegistry",
    "default_command_registry",
    "slash_command",
    "dispatch",
]

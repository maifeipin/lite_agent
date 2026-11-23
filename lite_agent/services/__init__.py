"""
Lite Agent 通用平台服务层 (Services)。

提供指令、工作流注册、路由分发、调度定时与通用机制。
依赖 contracts 与通用工具库，绝对不反向依赖 features 或 skills 业务逻辑。
"""

from lite_agent.services.commands import (
    CommandRegistry,
    default_command_registry,
    slash_command,
    dispatch,
)
from lite_agent.services.workflows import (
    WorkflowRegistry,
    default_workflow_registry,
)
from lite_agent.services.routes import (
    RouteRegistry,
    default_route_registry,
)
from lite_agent.services.cron import (
    CronService,
    CronJob,
    default_cron_service,
)
from lite_agent.services.user_session import (
    UserSessionService,
    DEFAULT_USER_SCOPE,
)

__all__ = [
    "CommandRegistry",
    "default_command_registry",
    "slash_command",
    "dispatch",
    "WorkflowRegistry",
    "default_workflow_registry",
    "RouteRegistry",
    "default_route_registry",
    "CronService",
    "CronJob",
    "default_cron_service",
    "UserSessionService",
    "DEFAULT_USER_SCOPE",
]

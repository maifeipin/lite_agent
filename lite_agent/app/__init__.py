"""
Lite Agent 应用装配与宿主层 (App)。
统一负责基础内核、服务组件与业务插件的装配与生命周期。
"""

from lite_agent.app.bootstrap import Application, create_app
from lite_agent.app.lifecycle import LifecycleManager
from lite_agent.app.plugins import PluginContext, PluginLoader

__all__ = [
    "Application",
    "create_app",
    "LifecycleManager",
    "PluginContext",
    "PluginLoader",
]

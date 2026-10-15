"""
受控插件加载器与插件协议接口 (app.plugins)。
仅通过显式清单登记与依赖注入受控加载，严禁目录级盲扫或任意表达式执行。
"""

import importlib
import logging
from typing import Dict, Any, Optional, Set, Callable
from services.commands import CommandRegistry

logger = logging.getLogger("app.plugins")


class PluginContext:
    """
    提供给插件的受限宿主上下文。
    仅按需暴露命令登记、工具注册、生命周期等句柄，绝不暴露完整 Agent 私有对象或任意模块反射。
    """

    def __init__(
        self,
        command_registry: Optional[CommandRegistry] = None,
        skill_engine: Optional[Any] = None,
        lifecycle: Optional[Any] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.command_registry = command_registry
        self.skill_engine = skill_engine
        self.lifecycle = lifecycle
        self.config = config or {}

    def register_command(self, name: str, handler: Callable, **kwargs):
        """向宿主指令注册表登记一个 Slash 指令。"""
        if self.command_registry is None:
            raise RuntimeError("宿主未提供命令注册服务")
        self.command_registry.register(name, handler, **kwargs)

    def register_skill(self, name: str, skill_def: Dict[str, Any]):
        """向宿主技能引擎登记一个工具。"""
        if self.skill_engine is None:
            raise RuntimeError("宿主未提供工具注册服务")
        self.skill_engine.register_tool(name, skill_def)

    def register_cleanup(self, name: str, cleanup_fn: Callable[[], None]):
        """登记插件停用/关闭时的清理钩子。"""
        if self.lifecycle is None:
            raise RuntimeError("宿主未提供生命周期服务")
        self.lifecycle.register_resource(name, cleanup_fn)


class PluginLoader:
    """受控插件加载器。严格基于显式白名单与清单加载，阻断未受管动态导入。"""

    def __init__(self, context: PluginContext, allowed_entries: Optional[Set[str]] = None):
        self.context = context
        self.allowed_entries = frozenset(allowed_entries or ())
        self.loaded_plugins: Dict[str, Any] = {}

    def load_plugin_by_entry(self, entry_name: str) -> Any:
        """根据受控 entry 导入并执行其 register(context) 契约。"""
        if entry_name not in self.allowed_entries:
            raise PermissionError(f"未受管的插件入口拒绝加载: {entry_name}")

        if entry_name in self.loaded_plugins:
            return self.loaded_plugins[entry_name]
        if self.context.lifecycle is not None and self.context.lifecycle.is_shutdown:
            raise RuntimeError("宿主已关闭，不能加载插件")

        try:
            mod = importlib.import_module(entry_name)
        except Exception as e:
            raise ImportError(f"加载插件模块失败 [{entry_name}]: {e}") from e

        register = getattr(mod, "register", None)
        if not callable(register):
            raise TypeError(f"插件必须提供 register(context): {entry_name}")
        register(self.context)

        self.loaded_plugins[entry_name] = mod
        return mod

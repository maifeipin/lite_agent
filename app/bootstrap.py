"""
基础组件与应用组装入口 (app.bootstrap)。
统一管理内核运行时、通用服务、公共通道适配器与受控插件装配。
"""

from typing import Dict, Any, Optional
from services.commands import CommandRegistry
from app.lifecycle import LifecycleManager
from app.plugins import PluginContext, PluginLoader
from app.manifests import CORE_MODE_PLUGINS, FULL_MODE_PLUGINS


class Application:
    """Lite Agent 宿主应用实例，持有专属的服务与生命周期资源。"""

    def __init__(
        self,
        mode: str,
        config: Dict[str, Any],
        command_registry: CommandRegistry,
        skill_engine: Optional[Any],
        lifecycle: LifecycleManager,
        plugin_loader: PluginLoader,
    ):
        self.mode = mode
        self.config = config
        self.command_registry = command_registry
        self.skill_engine = skill_engine
        self.lifecycle = lifecycle
        self.plugin_loader = plugin_loader

    def shutdown(self):
        """释放应用所有注册的句柄与后台任务。"""
        return self.lifecycle.shutdown()


def create_app(
    mode: str = "full",
    config: Optional[Dict[str, Any]] = None,
    skill_engine: Optional[Any] = None,
) -> Application:
    """
    创建并装配应用实例。
    :param mode: "core" (纯基础模式) 或 "full" (完整模式)
    :param config: 配置字典
    :param skill_engine: 显式注入的技能引擎实例 (支持空工具集合)
    """
    if mode not in ("core", "full"):
        raise ValueError(f"不支持的应用模式: {mode}")
    cfg = config or {}
    cmd_registry = CommandRegistry()
    lifecycle = LifecycleManager()

    # 注册核心系统内置指令 (基础能力，不依赖任何业务)
    cmd_registry.register(
        '/new',
        lambda a, m, args: None,
        category='系统',
        description='重置当前会话记忆，开启全新对话上下文',
        show_in_dashboard=True,
    )

    ctx = PluginContext(
        command_registry=cmd_registry,
        skill_engine=skill_engine,
        lifecycle=lifecycle,
        config=cfg,
    )

    allowed_entries = {p["entry"] for p in (FULL_MODE_PLUGINS if mode == "full" else CORE_MODE_PLUGINS)}
    loader = PluginLoader(context=ctx, allowed_entries=allowed_entries)

    app = Application(
        mode=mode,
        config=cfg,
        command_registry=cmd_registry,
        skill_engine=skill_engine,
        lifecycle=lifecycle,
        plugin_loader=loader,
    )

    lifecycle.register_resource("command_registry", cmd_registry.clear)

    return app

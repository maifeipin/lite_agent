"""
基础组件与应用组装入口 (app.bootstrap)。
统一管理内核运行时、通用服务、公共通道适配器与受控插件装配。
"""

from typing import Dict, Any, Optional
from services.commands import CommandRegistry
from services.workflows import WorkflowRegistry
from services.routes import RouteRegistry
from services.cron import CronService
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
        workflow_registry: WorkflowRegistry,
        route_registry: RouteRegistry,
        cron_service: CronService,
        skill_engine: Optional[Any],
        lifecycle: LifecycleManager,
        plugin_loader: PluginLoader,
    ):
        self.mode = mode
        self.config = config
        self.command_registry = command_registry
        self.workflow_registry = workflow_registry
        self.route_registry = route_registry
        self.cron_service = cron_service
        self.skill_engine = skill_engine
        self.lifecycle = lifecycle
        self.plugin_loader = plugin_loader

    @property
    def loaded_plugins(self):
        return self.plugin_loader.loaded_plugins

    def shutdown(self):
        """释放应用所有注册的句柄与后台任务。"""
        return self.lifecycle.shutdown()


def _handle_new(agent=None, msg=None, args=None):
    """重置当前会话记忆上下文"""
    return None


def create_app(
    mode: str = "full",
    config: Optional[Dict[str, Any]] = None,
    skill_engine: Optional[Any] = None,
    agent: Optional[Any] = None,
) -> Application:
    """
    创建并装配应用实例。
    :param mode: "core" (纯基础模式) 或 "full" (完整模式)
    :param config: 配置字典
    :param skill_engine: 显式注入的技能引擎实例 (支持空工具集合)
    :param agent: 显式注入的 Agent 实例 (支持测试替身注入)
    """
    if mode not in ("core", "full"):
        raise ValueError(f"不支持的应用模式: {mode}")
    cfg = config or {}

    if skill_engine is None:
        from core.skill_engine import SkillEngine
        skill_engine = SkillEngine(tools={}, auto_load=False)

    cmd_registry = CommandRegistry()
    workflow_registry = WorkflowRegistry()
    route_registry = RouteRegistry()
    cron_service = CronService()
    lifecycle = LifecycleManager()

    # 注册核心系统内置指令 (基础能力，不依赖任何业务)
    cmd_registry.register(
        '/new',
        _handle_new,
        category='系统',
        description='重置当前会话记忆，开启全新对话上下文',
        show_in_dashboard=True,
    )

    # 注册核心系统内置免认证健康状态路由 (基础能力，不依赖任何业务)
    import json as _json

    def _handle_health(handler, parsed_url):
        handler.send_response(200)
        handler._send_cors_headers()
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        data = {
            "status": "healthy",
            "mode": mode,
            "version": "1.0.0",
        }
        handler.wfile.write(_json.dumps(data).encode("utf-8"))

    route_registry.register(
        "GET",
        "/health",
        _handle_health,
        auth_required=False,
        description="系统核心健康检查端点",
    )
    route_registry.register(
        "GET",
        "/api/v1/health",
        _handle_health,
        auth_required=False,
        description="系统核心健康检查端点",
    )

    if agent is None and "llm" in cfg:
        try:
            from agent import Agent
            agent = Agent(
                config=cfg,
                skill_engine=skill_engine,
                command_registry=cmd_registry,
                workflow_registry=workflow_registry,
                cron=cron_service,
                domain_map={},
            )
        except Exception:
            lifecycle.shutdown()
            raise

    ctx = PluginContext(
        command_registry=cmd_registry,
        skill_engine=skill_engine,
        workflow_registry=workflow_registry,
        route_registry=route_registry,
        cron_service=cron_service,
        lifecycle=lifecycle,
        config=cfg,
        plugin_name="host",
        agent=agent,
    )

    manifest_plugins = FULL_MODE_PLUGINS if mode == "full" else CORE_MODE_PLUGINS
    allowed_entries = {p["entry"] for p in manifest_plugins}
    loader = PluginLoader(context=ctx, allowed_entries=allowed_entries)

    app = Application(
        mode=mode,
        config=cfg,
        command_registry=cmd_registry,
        workflow_registry=workflow_registry,
        route_registry=route_registry,
        cron_service=cron_service,
        skill_engine=skill_engine,
        lifecycle=lifecycle,
        plugin_loader=loader,
    )
    app.agent = agent
    app.domain_registry = ctx.domain_registry

    lifecycle.register_resource("command_registry", cmd_registry.clear)
    lifecycle.register_resource("workflow_registry", workflow_registry.clear)
    lifecycle.register_resource("route_registry", route_registry.clear)
    lifecycle.register_resource("cron_service", lambda: (cron_service.stop(wait=True, timeout=2.0), cron_service.clear()))
    lifecycle.register_resource("domain_registry", ctx.domain_registry.clear)
    if skill_engine is not None and hasattr(skill_engine, "clear"):
        lifecycle.register_resource("skill_engine", skill_engine.clear)

    return app

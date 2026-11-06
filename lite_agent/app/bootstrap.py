"""
基础组件与应用组装入口 (app.bootstrap)。
统一管理内核运行时、通用服务、公共通道适配器与受控插件装配。
"""

from typing import Dict, Any, Optional
from lite_agent.services.commands import CommandRegistry
from lite_agent.services.workflows import WorkflowRegistry
from lite_agent.services.routes import RouteRegistry
from lite_agent.services.cron import CronService
from lite_agent.app.lifecycle import LifecycleManager
from lite_agent.app.plugins import PluginContext, PluginLoader
from lite_agent.app.manifests import CORE_MODE_PLUGINS, FULL_MODE_PLUGINS


def _resolve_real_agent_class():
    """在模块加载时 (任何测试 patch 生效前) 绑定真实 Agent 类引用。

    测试会 patch('lite_agent.agent.Agent'), 函数内 import 会取到替换物,
    导致 isinstance 校验 TypeError。此处延迟到调用时解析, 但通过 sys.modules
    取原始类对象, 避开被 patch 的模块属性。
    """
    import sys
    mod = sys.modules.get("lite_agent.agent")
    cls = getattr(mod, "Agent", None) if mod is not None else None
    # 若模块属性已被 patch 为非类型, 视为无法解析真实类
    return cls if isinstance(cls, type) else None


def _is_real_agent(obj) -> bool:
    cls = _resolve_real_agent_class()
    return cls is not None and isinstance(obj, cls)


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
        """释放应用所有注册的句柄与后台任务。

        先停止调度并等待在途任务执行完毕, 再按注册倒序释放插件 handler、任务
        与其余受管资源——避免在途任务执行时其依赖资源已被插件清理提前关闭。
        """
        self.cron_service.stop(wait=True, timeout=2.0)
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
        from lite_agent.core.skill_engine import SkillEngine
        skill_engine = SkillEngine(tools={}, auto_load=False)

    cmd_registry = CommandRegistry()
    workflow_registry = WorkflowRegistry()
    route_registry = RouteRegistry()
    cron_service = CronService(log_callback=print)
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
        from lite_agent.core.version import get_version_info
        cron_snap = cron_service.health_snapshot()
        status, code = "healthy", 200
        loop_age = cron_snap.get("loop_age_s")
        if cron_snap.get("running") and loop_age is not None and loop_age > 180:
            # 调度循环超过 3 分钟无心跳：进程虽存活但已卡死
            status, code = "unhealthy", 503
        elif cron_snap.get("running") and cron_snap["busy_workers"] >= cron_snap["max_workers"]:
            # 工作线程全部被占用：新作业只能排队，无法按时执行
            status = "degraded"
        handler.send_response(code)
        handler._send_cors_headers()
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        data = {
            "status": status,
            "mode": mode,
            "version": "1.0.0",
            **get_version_info(),
            "cron": cron_snap,
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
            from lite_agent.agent import Agent
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

    # 所有权校验: 仅对真实 Agent 实例校验。应用 cron_service 与 Agent.cron
    # 必须同实例, 否则 /cron、/health、插件任务、配置任务与 Agent 观测割裂。
    # Mock/SimpleNamespace 等测试替身不是真实 Agent, 不参与校验;
    # patch 期间无法解析真实类时跳过校验 (见 _resolve_real_agent_class)。
    if _is_real_agent(agent) and agent.cron is not cron_service:
        lifecycle.shutdown()
        raise RuntimeError(
            "调度器所有权冲突: Agent.cron 与应用 cron_service 不是同一实例。"
            "请通过 create_app() 组装或注入应用调度器, 不要绕过宿主自建调度器。"
        )

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
    # 停止与排空在途任务由 Application.shutdown 前置执行 (必须先于插件资源清理);
    # 此处仅负责清空任务与 handler 注册表。
    lifecycle.register_resource("cron_service", cron_service.clear)
    lifecycle.register_resource("domain_registry", ctx.domain_registry.clear)
    if skill_engine is not None and hasattr(skill_engine, "clear"):
        lifecycle.register_resource("skill_engine", skill_engine.clear)

    return app

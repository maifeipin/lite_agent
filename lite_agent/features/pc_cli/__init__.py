"""
PC CLI 手机管理中枢插件入口 (features.pc_cli).
遵循 PluginContext 显式注册与事务隔离规范，默认关闭（config.pc_cli.enabled == True 时启用）。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from lite_agent.features.pc_cli import routes
from lite_agent.features.pc_cli.favorites import PcCliFavoritesService
from lite_agent.features.pc_cli.hub import PcCliHub
from lite_agent.features.pc_cli.manager import PcCliManager
from lite_agent.features.pc_cli.notifier import PcCliNotifier
from lite_agent.features.pc_cli.repository import PcCliRepository
from lite_agent.features.pc_cli.ws_server import PcCliWsServer

logger = logging.getLogger("features.pc_cli")


def register(context: Any) -> None:
    """将 pc_cli 插件注册到宿主应用上下文。"""
    cfg = getattr(context, "config", {}) or {}
    pc_cfg = cfg.get("pc_cli", {}) if isinstance(cfg, dict) else {}

    # 默认关闭：未显式启用时跳过注册，零副作用
    if not pc_cfg.get("enabled", False):
        logger.info("PC CLI 功能已禁用 (config.pc_cli.enabled != True)，跳过插件注册")
        return

    db_path = pc_cfg.get("db_path") or "data/pc_cli.db"
    ws_host = str(pc_cfg.get("ws_host") or "127.0.0.1")
    ws_port = int(pc_cfg.get("ws_port", 8889))

    daemon_token_env = str(pc_cfg.get("daemon_token_env") or "PC_CLI_DAEMON_TOKEN")
    daemon_token = str(pc_cfg.get("daemon_token") or os.environ.get(daemon_token_env, "") or "")

    api_cfg = cfg.get("api", {}) if isinstance(cfg, dict) else {}
    client_token = str(
        pc_cfg.get("client_token")
        or api_cfg.get("auth_token")
        or os.environ.get("API_AUTH_TOKEN", "")
        or ""
    )

    send_owner = getattr(context, "send_owner_response", None)
    notifier = PcCliNotifier(
        send_fn=send_owner if callable(send_owner) else None,
        notify_on_complete=bool(pc_cfg.get("notify_on_complete", True)),
        notify_on_approval=bool(pc_cfg.get("notify_on_approval", True)),
        mobile_base_url=str(pc_cfg.get("mobile_base_url") or ""),
    )

    repo = PcCliRepository(db_path=db_path)
    hub = PcCliHub(repo=repo, notifier=notifier.notify_async)
    agent = getattr(context, "agent", None)
    gateway = getattr(agent, "llm", None) or getattr(agent, "gateway", None)
    router = None
    if agent is not None:
        get_router = getattr(agent, "_get_request_model_router", None)
        if callable(get_router):
            try:
                router = get_router()
            except Exception as e:
                logger.warning(f"从宿主获取 _get_request_model_router 失败: {e}")
        if router is None:
            router = getattr(agent, "_request_model_router", None) or getattr(agent, "router", None)
    if router is None:
        router = getattr(gateway, "router", None)
    default_model = (
        str(pc_cfg.get("manager_model") or "")
        or str(getattr(router, "default_model", "") or "")
        or str((cfg.get("llm", {}).get("default") if isinstance(cfg, dict) else "") or "")
    )
    manager = PcCliManager(hub=hub, config=cfg, gateway=gateway, router=router, default_model=default_model)
    favorites = PcCliFavoritesService(config=cfg)
    ws_server: Optional[PcCliWsServer] = None

    if pc_cfg.get("start_ws_server", True):
        session_verifier = None
        try:
            from lite_agent.services.user_session import UserSessionService

            session_service = getattr(agent, "user_session_service", None)
            if session_service is None:
                session_cfg = getattr(agent, "_config", None) or cfg
                session_service = UserSessionService(config=session_cfg)
                if agent is not None:
                    try:
                        agent.user_session_service = session_service
                    except Exception:
                        pass
            if session_service.is_available():
                session_verifier = lambda tok: bool(
                    session_service.verify_access_token(tok, required_scope="user:general")[0]
                )
        except Exception as e:
            logger.warning(f"PC CLI WebSocket 会话鉴权初始化失败，将回退静态 Token 鉴权: {e}")

        ws_server = PcCliWsServer(
            hub=hub,
            host=ws_host,
            port=ws_port,
            daemon_token=daemon_token,
            client_token=client_token,
            client_token_verifier=session_verifier,
        )
        ws_server.start()
        if agent is not None:
            try:
                agent.pc_cli_ws_server = ws_server
            except Exception:
                pass

    if hasattr(context, "register_route"):
        context.register_route(
            "GET",
            "/api/v1/pc-cli/nodes",
            lambda h, u: routes.handle_get_nodes(h, u, hub),
            description="获取 PC 节点在线列表",
        )
        context.register_route(
            "ALL",
            "/api/v1/pc-cli/nodes/",
            lambda h, u: routes.handle_nodes_sub_dispatch(h, u, hub),
            is_prefix=True,
            description="PC 节点目录浏览子端点",
        )
        context.register_route(
            "GET",
            "/api/v1/pc-cli/bots",
            lambda h, u: routes.handle_get_bots(h, u, hub),
            description="获取 PC CLI Bot 列表",
        )
        context.register_route(
            "POST",
            "/api/v1/pc-cli/bots",
            lambda h, u: routes.handle_post_bots(h, u, hub),
            description="新建 PC CLI Bot",
        )
        context.register_route(
            "ALL",
            "/api/v1/pc-cli/bots/",
            lambda h, u: routes.handle_bots_sub_dispatch(h, u, hub),
            is_prefix=True,
            description="Bot 详情、事件流、下发指令与中止子端点",
        )
        context.register_route(
            "GET",
            "/api/v1/pc-cli/approvals",
            lambda h, u: routes.handle_get_approvals(h, u, hub),
            description="获取待处理审批列表",
        )
        context.register_route(
            "ALL",
            "/api/v1/pc-cli/approvals/",
            lambda h, u: routes.handle_approvals_sub_dispatch(h, u, hub),
            is_prefix=True,
            description="提交权限审批决策",
        )
        context.register_route(
            "GET",
            "/api/v1/pc-cli/models",
            lambda h, u: routes.handle_get_models(h, u, manager),
            description="获取 conf.d/llm.json 脱敏模型目录供总管切换",
        )
        context.register_route(
            "GET",
            "/api/v1/pc-cli/stats",
            lambda h, u: routes.handle_get_stats(h, u, hub),
            description="获取多 PC CLI 总体统计看板数据",
        )
        context.register_route(
            "POST",
            "/api/v1/pc-cli/manager/chat",
            lambda h, u: routes.handle_post_manager_chat(h, u, manager),
            description="与 LLM 总管对话调度 Bot 或发起授权单",
        )
        context.register_route(
            "GET",
            "/api/v1/pc-cli/favorites",
            lambda h, u: routes.handle_get_favorites(h, u, favorites),
            description="查询或检索 PC Bots 知识库收藏",
        )
        context.register_route(
            "POST",
            "/api/v1/pc-cli/favorites",
            lambda h, u: routes.handle_post_favorites(h, u, hub, favorites),
            description="收藏或追加 PC Bots 回复到个人知识库",
        )
        context.register_route(
            "ALL",
            "/api/v1/pc-cli/favorites/",
            lambda h, u: routes.handle_favorites_sub_dispatch(h, u, favorites),
            is_prefix=True,
            description="单条收藏读取、取消收藏及索引重建子端点",
        )

    def _cleanup() -> None:
        if ws_server is not None:
            ws_server.stop()
        notifier.close()
        hub.close()

    if hasattr(context, "record_cleanup"):
        context.record_cleanup(_cleanup, name="pc_cli_cleanup")
    elif hasattr(context, "register_cleanup"):
        context.register_cleanup("pc_cli_cleanup", _cleanup)

"""
Network Ops 网络代理与边缘节点运维模块 (features.network_ops).
管理 Socks5 代理节点配置、连通性探测以及 Edge 边缘节点通信。
实现受控插件 register(context) 契约。
"""

from typing import Any

SOCKS5_DOMAIN_SPEC = {
    "pattern": r"(代理|socks5|节点|出站)",
    "default_tools": [
        "socks5_list", "socks5_get_active",
        "socks5_test", "socks5_test_outbound",
    ],
    "explicit_intent_tools": [
        "socks5_add", "socks5_update", "socks5_delete",
        "socks5_set_active",
    ],
    "explicit_patterns": r"(添加|新增|删除|切换|设为.*主节点|更新.*节点)",
}

EDGE_DOMAIN_SPEC = {
    "pattern": r"(边缘|edge|fleet|节点巡检|下发命令)",
    "default_tools": [
        "fleet_audit", "edge_health", "edge_query",
    ],
    "explicit_intent_tools": [
        "edge_cmd", "edge_sweep",
    ],
    "explicit_patterns": r"(下发命令|清理|巡检|扫描)",
}



def register(context: Any) -> None:
    """注册 Network Ops (Socks5 / Edge) 插件到宿主上下文。"""
    from skills import ops_socks5, ops_edge_cmd, ops_edge_health, ops_fleet_audit
    from core.skill_engine import _skill_registry

    # 1. 注册网络与边缘运维技能
    net_tools = [
        "socks5_list", "socks5_add", "socks5_set_active", "socks5_get_active",
        "socks5_update", "socks5_delete", "socks5_test", "socks5_test_outbound",
        "edge_cmd", "edge_query", "edge_sweep", "edge_health", "fleet_audit"
    ]
    for t_name in net_tools:
        if t_name in _skill_registry:
            context.register_skill(t_name, _skill_registry[t_name])



    # 2. 注册 Socks5 路由
    from features.network_ops import socks5_adapter

    def _handle_socks5_route(http_handler: Any, parsed_url: Any):
        method = http_handler.command
        req_path = parsed_url.path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'

        if method == "GET":
            if req_path == "/api/v1/socks5/active":
                socks5_adapter.handle_socks5_active_get(http_handler, parsed_url)
            elif req_path == "/api/v1/socks5/health":
                socks5_adapter.handle_socks5_health(http_handler, parsed_url)
            elif req_path == "/api/v1/socks5/test":
                socks5_adapter.handle_socks5_test(http_handler, parsed_url)
            elif req_path == "/api/v1/socks5/script":
                socks5_adapter.handle_socks5_script(http_handler, parsed_url)
            else:
                socks5_adapter.handle_socks5_get(http_handler, parsed_url)
        elif method == "POST":
            if req_path == "/api/v1/socks5/active":
                socks5_adapter.handle_socks5_active_post(http_handler, parsed_url)
            else:
                socks5_adapter.handle_socks5_post(http_handler, parsed_url)
        elif method == "PATCH":
            socks5_adapter.handle_socks5_patch(http_handler, parsed_url)
        elif method == "DELETE":
            socks5_adapter.handle_socks5_delete(http_handler, parsed_url)
        else:
            http_handler.send_error(405, "Method Not Allowed")

    context.register_route("ALL", "/api/v1/socks5", _handle_socks5_route, is_prefix=True)

    # 3. 注册 Edge 节点通信路由
    from features.network_ops import edge_adapter

    def _handle_edge_route(http_handler: Any, parsed_url: Any):
        req_path = parsed_url.path
        if req_path == "/api/pull_task":
            edge_adapter.handle_pull_task(http_handler, parsed_url)
        elif req_path == "/api/report":
            edge_adapter.handle_edge_report(http_handler, parsed_url)
        elif req_path == "/api/task_result":
            edge_adapter.handle_task_result(http_handler, parsed_url)
        elif req_path == "/api/edge_task":
            edge_adapter.handle_edge_task(http_handler, parsed_url)
        else:
            http_handler.send_error(404, "Not Found")

    context.register_route("ALL", "/api/pull_task", _handle_edge_route)
    context.register_route("ALL", "/api/report", _handle_edge_route)
    context.register_route("ALL", "/api/task_result", _handle_edge_route)
    context.register_route("ALL", "/api/edge_task", _handle_edge_route)

    # 4. 注册定时作业 (边缘健康巡检)
    from features.network_ops import edge_service
    context.register_cron("edge_health", "*/10 * * * *", edge_service.edge_health)

    # 5. 注册 Socks5 自动故障转移后台守护 (由 network_ops 插件拥有该能力与生命周期)
    net_cfg = getattr(context, "config", {}) or {}
    socks_cfg = net_cfg.get("network_ops") or net_cfg.get("socks5") or net_cfg.get("network") or {}
    if socks_cfg.get("failover_enabled", True):
        from features.network_ops import socks5_service
        stop_worker = socks5_service.start_failover_worker()
        if hasattr(context, "record_cleanup"):
            context.record_cleanup(stop_worker, "socks5_failover_worker")
        elif hasattr(context, "register_cleanup"):
            context.register_cleanup("socks5_failover_worker", stop_worker)


    # 6. 注册网络代理与边缘节点 RequestSelector 领域路由
    if hasattr(context, "register_domain"):
        context.register_domain("socks5", SOCKS5_DOMAIN_SPEC)
        context.register_domain("edge", EDGE_DOMAIN_SPEC)


__all__ = ["register", "SOCKS5_DOMAIN_SPEC", "EDGE_DOMAIN_SPEC"]

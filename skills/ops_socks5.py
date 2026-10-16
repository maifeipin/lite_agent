"""
Skills Ops Socks5 (skills.ops_socks5).
Socks5 代理模型工具薄入口（保留 @skill 契约与薄委托）。
业务、数据库与后台 Worker 守护主体位于 canonical 服务 features/network_ops/socks5_service.py。
"""

import sys, os, threading
from typing import Optional, Callable, Tuple, Any, Dict, List, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from features.network_ops import socks5_service

# 导入所有 canonical 常用函数以支持静态导出与调用
from features.network_ops.socks5_service import (
    LOCAL_PORT_EXCLUDES,
    get_socks5_db_path,
    _ensure_socks5_schema,
    get_socks5_proxies,
    get_socks5_proxy_by_id,
    get_current_active_proxy,
    add_socks5_proxy,
    update_socks5_proxy,
    delete_socks5_proxy,
    apply_active_proxy_to_vps1,
    test_socks5_host,
    test_socks5_outbound_http,
    test_socks5_proxy_outbound,
    failover_worker_loop,
    stop_failover_worker,
    generate_ps1_script,
    generate_sh_script,
    parse_host_port,
    parse_runcmd_to_naive_config,
)


def start_failover_worker(
    poll_interval: float = 180.0,
    probe_fn: Optional[Callable[[], dict]] = None,
    apply_fn: Optional[Callable[[int], Tuple[bool, str]]] = None,
    alert_fn: Optional[Callable[..., None]] = None,
    stop_event: Optional[threading.Event] = None,
    get_proxies_fn: Optional[Callable[[], List[dict]]] = None,
    get_active_fn: Optional[Callable[[], Optional[dict]]] = None,
) -> Callable[[], None]:
    """薄委托：将外部/测试参数显式单向转发至 canonical 服务，绝不污染 canonical 模块。"""
    return socks5_service.start_failover_worker(
        poll_interval=poll_interval,
        probe_fn=probe_fn,
        apply_fn=apply_fn,
        alert_fn=alert_fn,
        stop_event=stop_event,
        get_proxies_fn=get_proxies_fn,
        get_active_fn=get_active_fn,
    )


# PEP 562 模块级延迟只读代理（仅用于读取 canonical 模块中的动态属性，如 _failover_worker_thread 等）
# 绝不拦截 __setattr__，绝不覆盖模块 class，绝不污染 canonical 服务
def __getattr__(name: str) -> Any:
    if hasattr(socks5_service, name):
        return getattr(socks5_service, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


# ── Agent Skills (薄工具入口，保留 schema 与参数) ──

@skill(name="socks5_list", description="列出所有 Socks5 代理节点配置（支持关键词过滤）")
def ops_socks5_list(query: str = "") -> str:
    """列出所有 Socks5 代理节点配置（支持关键词过滤）。"""
    proxies = socks5_service.get_socks5_proxies(query)
    if not proxies:
        return "未找到匹配的 Socks5 代理节点。"
    
    lines = [f"| ID | 状态 | 节点名称 | 服务器 IP/Host | 客户端代理 | 备注 |", "|---|---|---|---|---|---|"]
    for p in proxies:
        active_tag = "🟢 主节点" if p.get("is_active") == 1 else "⚪ 备用"
        lines.append(f"| {p['id']} | {active_tag} | {p['servername']} | `{p['host']}` | `{p['clientproxy']}` | {p['memo']} |")
    return "\n".join(lines)


@skill(name="socks5_add", description="新增 Socks5 代理节点配置")
def ops_socks5_add(servername: str, host: str, runcmd: str = "", clientproxy: str = "", memo: str = "", priority: int = 0) -> str:
    """新增 Socks5 代理节点配置。"""
    if not servername or not host:
        return "错误：节点名称(servername)和主机地址(host)为必填项。"
    new_id = socks5_service.add_socks5_proxy(host, runcmd, servername, clientproxy, memo, priority)
    return f"成功添加 Socks5 节点 [ID: {new_id}] {servername} ({host})"


@skill(name="socks5_set_active", description="将指定的 Socks5 代理节点切换为 VPS1 的当前生效主节点")
def ops_socks5_set_active(id: int) -> str:
    """将指定的 Socks5 代理节点切换为 VPS1 的当前生效主节点。"""
    ok, msg = socks5_service.apply_active_proxy_to_vps1(id)
    return msg


@skill(name="socks5_get_active", description="获取 VPS1 当前生效的 Socks5 代理主节点信息")
def ops_socks5_get_active() -> str:
    """获取 VPS1 当前生效的 Socks5 代理主节点信息。"""
    active = socks5_service.get_current_active_proxy()
    if not active:
        return "当前未选择生效的 Socks5 主节点。"
    return f"VPS1 当前主节点 [ID: {active['id']}] {active['servername']} ({active['host']})"


@skill(name="socks5_update", description="更新已有的 Socks5 代理节点配置")
def ops_socks5_update(id: int, servername: str = None, host: str = None, runcmd: str = None, clientproxy: str = None, memo: str = None, priority: int = None) -> str:
    """更新已有的 Socks5 代理节点配置。"""
    ok = socks5_service.update_socks5_proxy(id, host, runcmd, servername, clientproxy, memo, priority)
    if ok:
        return f"成功更新 Socks5 节点 [ID: {id}]"
    return f"更新失败：未找到 ID 为 {id} 的节点。"


@skill(name="socks5_delete", description="删除指定的 Socks5 代理节点配置")
def ops_socks5_delete(id: int) -> str:
    """删除指定的 Socks5 代理节点配置。"""
    ok = socks5_service.delete_socks5_proxy(id)
    if ok:
        return f"成功删除 Socks5 节点 [ID: {id}]"
    return f"删除失败：未找到 ID 为 {id} 的节点。"


@skill(name="socks5_test", description="测试指定 Socks5 节点的服务器 TCP 连通性")
def ops_socks5_test(id: int) -> str:
    """测试指定 Socks5 节点的服务器 TCP 连通性。"""
    proxy = socks5_service.get_socks5_proxy_by_id(id)
    if not proxy:
        return f"测试失败：未找到 ID 为 {id} 的节点。"
    res = socks5_service.test_socks5_host(host=proxy["host"], runcmd=proxy.get("runcmd", ""))
    if res["success"]:
        return f"节点 [{proxy['servername']}] (端口 {res['port']}) 连通正常，延迟: {res['latency_ms']} ms"
    return f"节点 [{proxy['servername']}] (端口 {res['port']}) 无法连接：{res.get('error', '未知错误')}"


@skill(
    name="socks5_test_outbound", 
    description="测试 Socks5 代理节点的真实 HTTP 出站网络连通性与数据包公网出口 IP 回显",
    params={"id": {"type": "integer", "description": "指定测试的节点 ID，留空则测试当前 VPS1 生效的主节点 (18988)", "default": None}}
)
def ops_socks5_test_outbound(id: int = None) -> str:
    """测试指定节点（或当前在用主节点）的真实 HTTP 出站网络连通测试与出口 IP 回显。"""
    if id is not None:
        res = socks5_service.test_socks5_proxy_outbound(id)
        node_prefix = f"节点 [ID:{id}] "
    else:
        res = socks5_service.test_socks5_outbound_http()
        node_prefix = "VPS1 当前生效代理 (18988) "
        
    if res["success"]:
        ip_info = f"，真实出站 IP: {res['ip']}" if res.get("ip") else ""
        loc_info = f" [{res['loc']}]" if res.get("loc") else ""
        return f"{node_prefix}出站访问正常 (HTTP {res['http_code']}){ip_info}{loc_info}，端到端延迟: {res['latency_ms']} ms"
    return f"{node_prefix}出站访问异常: {res.get('error')}"


# 向后兼容别名与函数映射
socks5_list = ops_socks5_list
socks5_add = ops_socks5_add
socks5_set_active = ops_socks5_set_active
socks5_get_active = ops_socks5_get_active
socks5_update = ops_socks5_update
socks5_delete = ops_socks5_delete
socks5_test = ops_socks5_test
socks5_test_outbound = ops_socks5_test_outbound

__all__ = [
    "ops_socks5_list",
    "ops_socks5_add",
    "ops_socks5_set_active",
    "ops_socks5_get_active",
    "ops_socks5_update",
    "ops_socks5_delete",
    "ops_socks5_test",
    "ops_socks5_test_outbound",
    "socks5_list",
    "socks5_add",
    "socks5_set_active",
    "socks5_get_active",
    "socks5_update",
    "socks5_delete",
    "socks5_test",
    "socks5_test_outbound",
    "apply_active_proxy_to_vps1",
    "test_socks5_host",
    "test_socks5_outbound_http",
    "test_socks5_proxy_outbound",
    "get_socks5_proxies",
    "get_socks5_proxy_by_id",
    "get_current_active_proxy",
    "add_socks5_proxy",
    "update_socks5_proxy",
    "delete_socks5_proxy",
    "start_failover_worker",
    "stop_failover_worker",
    "failover_worker_loop",
    "generate_ps1_script",
    "generate_sh_script",
    "parse_host_port",
    "parse_runcmd_to_naive_config",
]

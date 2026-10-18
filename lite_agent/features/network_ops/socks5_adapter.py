"""
Socks5 运维业务 HTTP Adapter 模块 (features.network_ops.socks5_adapter).
负责 Socks5 节点查询、添加、修改、测试、脚本生成与健康检查等业务逻辑，
使公共通道彻底摆脱特定运维实现。
所有调用全部通过 canonical 服务 features.network_ops.socks5_service 进行。
"""

import json
from urllib.parse import parse_qs
from typing import Any
from lite_agent.features.network_ops import socks5_service


def handle_socks5_get(http_handler: Any, parsed_url: Any) -> None:
    qs = parse_qs(parsed_url.query)
    q = qs.get("q", [None])[0]
    try:
        proxies = socks5_service.get_socks5_proxies(query=q)
        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"success": True, "data": proxies}).encode('utf-8'))
    except Exception as e:
        http_handler.send_error(500, str(e))


def handle_socks5_post(http_handler: Any, parsed_url: Any) -> None:
    content_length = int(http_handler.headers.get('Content-Length', 0))
    if content_length == 0:
        http_handler.send_error(400, "Bad Request: Empty body")
        return
    body = http_handler.rfile.read(content_length)
    try:
        data = json.loads(body.decode('utf-8'))
        servername = data.get("servername")
        host = data.get("host")
        if not servername or not host:
            http_handler.send_error(400, "Bad Request: Missing servername or host")
            return
        runcmd = data.get("runcmd", "")
        clientproxy = data.get("clientproxy", "")
        memo = data.get("memo", "")

        new_id = socks5_service.add_socks5_proxy(host=host, runcmd=runcmd, servername=servername, clientproxy=clientproxy, memo=memo)
        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"success": True, "id": new_id}).encode('utf-8'))
    except Exception as e:
        http_handler.send_error(500, str(e))


def handle_socks5_patch(http_handler: Any, parsed_url: Any) -> None:
    path = parsed_url.path
    proxy_id_str = path.rsplit('/', 1)[-1]
    try:
        proxy_id = int(proxy_id_str)
    except ValueError:
        http_handler.send_error(400, "Invalid proxy ID")
        return
    content_length = int(http_handler.headers.get('Content-Length', 0))
    body = http_handler.rfile.read(content_length) if content_length > 0 else b"{}"
    try:
        data = json.loads(body.decode('utf-8'))
        ok = socks5_service.update_socks5_proxy(
            proxy_id=proxy_id,
            host=data.get("host"),
            runcmd=data.get("runcmd"),
            servername=data.get("servername"),
            clientproxy=data.get("clientproxy"),
            memo=data.get("memo")
        )
        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"success": ok}).encode('utf-8'))
    except Exception as e:
        http_handler.send_error(500, str(e))


def handle_socks5_delete(http_handler: Any, parsed_url: Any) -> None:
    path = parsed_url.path
    proxy_id_str = path.rsplit('/', 1)[-1]
    try:
        proxy_id = int(proxy_id_str)
    except ValueError:
        http_handler.send_error(400, "Invalid proxy ID")
        return
    try:
        ok = socks5_service.delete_socks5_proxy(proxy_id)
        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"success": ok}).encode('utf-8'))
    except Exception as e:
        http_handler.send_error(500, str(e))


def handle_socks5_test(http_handler: Any, parsed_url: Any) -> None:
    qs = parse_qs(parsed_url.query)
    proxy_id_str = qs.get("id", [None])[0]
    host = qs.get("host", [None])[0]
    port_str = qs.get("port", [None])[0]
    port = int(port_str) if port_str and port_str.isdigit() else None

    runcmd = ""
    if proxy_id_str and proxy_id_str.isdigit():
        proxy = socks5_service.get_socks5_proxy_by_id(int(proxy_id_str))
        if proxy:
            host = proxy.get("host")
            runcmd = proxy.get("runcmd", "")

    if not host:
        http_handler.send_error(400, "Missing host or valid proxy id")
        return

    res = socks5_service.test_socks5_host(host=host, runcmd=runcmd, port=port)
    http_handler.send_response(200)
    if hasattr(http_handler, "_send_cors_headers"):
        http_handler._send_cors_headers()
    http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
    http_handler.end_headers()
    http_handler.wfile.write(json.dumps({"success": res["success"], "result": res}).encode('utf-8'))


def handle_socks5_script(http_handler: Any, parsed_url: Any) -> None:
    qs = parse_qs(parsed_url.query)
    proxy_id_str = qs.get("id", [None])[0]
    script_type = qs.get("type", ["ps1"])[0].lower()
    if not proxy_id_str or not proxy_id_str.isdigit():
        http_handler.send_error(400, "Missing proxy id")
        return

    proxy = socks5_service.get_socks5_proxy_by_id(int(proxy_id_str))
    if not proxy:
        http_handler.send_error(404, "Proxy not found")
        return

    if script_type == "sh":
        script_content = socks5_service.generate_sh_script(proxy)
    else:
        script_content = socks5_service.generate_ps1_script(proxy)

    http_handler.send_response(200)
    if hasattr(http_handler, "_send_cors_headers"):
        http_handler._send_cors_headers()
    http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
    http_handler.end_headers()
    http_handler.wfile.write(json.dumps({"success": True, "type": script_type, "script": script_content}).encode('utf-8'))


def handle_socks5_active_get(http_handler: Any, parsed_url: Any) -> None:
    active = socks5_service.get_current_active_proxy()
    http_handler.send_response(200)
    if hasattr(http_handler, "_send_cors_headers"):
        http_handler._send_cors_headers()
    http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
    http_handler.end_headers()
    http_handler.wfile.write(json.dumps({"success": True, "data": active}).encode('utf-8'))


def handle_socks5_active_post(http_handler: Any, parsed_url: Any) -> None:
    if getattr(http_handler, 'is_edge', False) or getattr(http_handler, 'is_guest', False):
        http_handler.send_error(403, "Forbidden: Only master token can perform active proxy switching")
        return
    content_length = int(http_handler.headers.get('Content-Length', 0))
    if content_length == 0:
        http_handler.send_error(400, "Bad Request: Empty body")
        return
    body = http_handler.rfile.read(content_length)
    try:
        data = json.loads(body.decode('utf-8'))
        proxy_id = data.get("id")
        if not proxy_id:
            http_handler.send_error(400, "Missing proxy id")
            return
        ok, msg = socks5_service.apply_active_proxy_to_vps1(int(proxy_id))
        http_handler.send_response(200 if ok else 400)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"success": ok, "message": msg}).encode('utf-8'))
    except Exception as e:
        http_handler.send_error(500, str(e))


def handle_socks5_health(http_handler: Any, parsed_url: Any) -> None:
    try:
        qs = parse_qs(parsed_url.query)
        proxy_id_list = qs.get("id") or qs.get("proxy_id")

        if proxy_id_list and proxy_id_list[0].isdigit():
            res = socks5_service.test_socks5_proxy_outbound(int(proxy_id_list[0]))
        else:
            res = socks5_service.test_socks5_outbound_http()

        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"success": res.get("success", False), "result": res}).encode('utf-8'))
    except Exception as e:
        http_handler.send_error(500, str(e))

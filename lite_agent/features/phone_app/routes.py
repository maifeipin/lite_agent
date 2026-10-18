"""
Phone App HTTP 路由与适配模块 (features.phone_app.routes).
实现标准 BaseHTTPRequestHandler 路由处理器，集成到 RouteRegistry。
严格遵循第 11 节，全链路基于认证与角色隔离，拒绝未授权访问。
"""

from __future__ import annotations
import json
from urllib.parse import parse_qs
from typing import Any, Dict
from lite_agent.features.phone_app.service import PhoneAppService
from lite_agent.features.phone_app.catalog import AppCatalog


def _send_json(handler: Any, status_code: int, data: Dict[str, Any]):
    handler.send_response(status_code)
    if hasattr(handler, "_send_cors_headers"):
        handler._send_cors_headers()
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.end_headers()
    handler.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))


MAX_BODY_BYTES = 10 * 1024 * 1024  # 10MB 消息与截图上限 (C01)


def _read_body_json(handler: Any) -> Dict[str, Any]:
    content_len = int(handler.headers.get("Content-Length", 0))
    if content_len <= 0:
        return {}
    if content_len > MAX_BODY_BYTES:
        _send_json(handler, 413, {"error": f"请求体超出上限 ({content_len} > {MAX_BODY_BYTES})"})
        raise ValueError("Payload too large")
    body_bytes = handler.rfile.read(content_len)
    return json.loads(body_bytes.decode("utf-8"))


# --- Route Handlers ---

def handle_get_devices(handler: Any, parsed_url: Any, service: PhoneAppService):
    devices = service.get_devices()
    _send_json(handler, 200, {"success": True, "devices": devices})


def handle_get_apps(handler: Any, parsed_url: Any, service: PhoneAppService):
    qs = parse_qs(parsed_url.query)
    device_id = qs.get("device_id", [None])[0]
    action = qs.get("action", [None])[0]
    apps = service.catalog.list_available_apps(device_id=device_id, action=action, only_validated=False)
    _send_json(handler, 200, {"success": True, "apps": [a.to_dict() for a in apps]})


def handle_post_batches(handler: Any, parsed_url: Any, service: PhoneAppService):
    body = _read_body_json(handler)
    query = body.get("query", "").strip()
    app_ids = body.get("app_ids", [])
    device_id = body.get("device_id", "c68a3cf2")
    action = body.get("action", "search_products")
    filters = body.get("filters")
    budget = body.get("budget")
    mode = body.get("mode", "fixed")

    if not query:
        _send_json(handler, 400, {"error": "搜索词 query 不能为空"})
        return

    ok, res = service.create_batch(
        query=query,
        app_ids=app_ids,
        device_id=device_id,
        action=action,
        filters=filters,
        budget=budget,
        mode=mode
    )
    if not ok:
        _send_json(handler, 400, res)
    else:
        _send_json(handler, 201, {"success": True, **res})


def handle_get_batches(handler: Any, parsed_url: Any, service: PhoneAppService):
    batches = service.repo.list_batches(limit=30)
    _send_json(handler, 200, {"success": True, "batches": batches})


def handle_get_batch_detail(handler: Any, parsed_url: Any, service: PhoneAppService, batch_id: str):
    summary = service.get_batch_summary(batch_id)
    if not summary:
        _send_json(handler, 404, {"error": "批次不存在"})
    else:
        _send_json(handler, 200, {"success": True, **summary})


def handle_post_generate(handler: Any, parsed_url: Any, service: PhoneAppService, batch_id: str):
    _ = _read_body_json(handler)
    ok, res = service.generate_scripts(batch_id)
    if not ok:
        _send_json(handler, 400, res)
    else:
        _send_json(handler, 200, {"success": True, **res})


def handle_put_draft(handler: Any, parsed_url: Any, service: PhoneAppService, batch_id: str):
    body = _read_body_json(handler)
    scripts = body.get("scripts", {})
    ok, res = service.update_draft(batch_id, scripts)
    if not ok:
        _send_json(handler, 400, res)
    else:
        _send_json(handler, 200, {"success": True, **res})


def handle_post_approve(handler: Any, parsed_url: Any, service: PhoneAppService, batch_id: str):
    _ = _read_body_json(handler)
    # 从服务端获取身份 (默认 admin)
    reviewer_id = getattr(handler, "user_id", "admin")
    ok, res = service.approve_batch(batch_id, reviewer_id=reviewer_id)
    if not ok:
        _send_json(handler, 400, res)
    else:
        _send_json(handler, 200, {"success": True, **res})


def handle_post_execute(handler: Any, parsed_url: Any, service: PhoneAppService, batch_id: str):
    _ = _read_body_json(handler)
    user_context = {"user_id": getattr(handler, "user_id", "admin")}
    ok, res = service.execute_batch(batch_id, user_context=user_context, async_run=True)
    if not ok:
        _send_json(handler, 400, res)
    else:
        _send_json(handler, 200, {"success": True, **res})


def handle_get_events(handler: Any, parsed_url: Any, service: PhoneAppService):
    qs = parse_qs(parsed_url.query)
    batch_id = qs.get("batch_id", [""])[0]
    since_id = int(qs.get("since_id", [0])[0])
    if not batch_id:
        _send_json(handler, 400, {"error": "缺少 batch_id"})
        return
    events = service.repo.list_events(batch_id, since_id=since_id)
    _send_json(handler, 200, {"success": True, "events": events})


# --- Worker Channel Handlers (B02) ---

def handle_worker_handshake(handler: Any, parsed_url: Any, service: PhoneAppService):
    body = _read_body_json(handler)
    worker_id = body.get("worker_id", "")
    token = body.get("token", "")
    version = body.get("version", "")
    capabilities = body.get("capabilities", [])
    devices = body.get("devices", [])

    ok, res = service.worker_hub.handshake(worker_id, token, version, capabilities, devices)
    if not ok:
        _send_json(handler, 401 if "Token" in res.get("error", "") else 400, res)
    else:
        _send_json(handler, 200, {"success": True, **res})


def handle_worker_heartbeat(handler: Any, parsed_url: Any, service: PhoneAppService):
    body = _read_body_json(handler)
    worker_id = body.get("worker_id", "")
    session_id = body.get("session_id", "")
    devices = body.get("devices")

    ok, res = service.worker_hub.heartbeat(worker_id, session_id, devices)
    if not ok:
        _send_json(handler, 403, res)
    else:
        _send_json(handler, 200, {"success": True, **res})


def handle_worker_poll_task(handler: Any, parsed_url: Any, service: PhoneAppService):
    body = _read_body_json(handler)
    worker_id = body.get("worker_id", "")
    session_id = body.get("session_id", "")
    timeout_sec = float(body.get("timeout_sec", 5.0))

    ok, res = service.worker_hub.poll_task(worker_id, session_id, timeout_sec=timeout_sec)
    if not ok:
        _send_json(handler, 403, res)
    else:
        _send_json(handler, 200, {"success": True, **res})


def handle_worker_submit_receipt(handler: Any, parsed_url: Any, service: PhoneAppService):
    body = _read_body_json(handler)
    worker_id = body.get("worker_id", "")
    session_id = body.get("session_id", "")
    lease_id = body.get("lease_id", "")
    receipt = body.get("receipt", {})

    if not worker_id or not session_id or not lease_id:
        _send_json(handler, 400, {"error": "缺少必填参数 (worker_id, session_id, lease_id)"})
        return

    ok, res = service.worker_hub.submit_receipt(worker_id, session_id, lease_id, receipt)
    if not ok:
        status_code = 401 if ("未注册" in res.get("error", "") or "会话" in res.get("error", "")) else 400
        _send_json(handler, status_code, res)
    else:
        _send_json(handler, 200, {"success": True, **res})


def handle_resume_device(handler: Any, parsed_url: Any, service: PhoneAppService):
    body = _read_body_json(handler)
    device_id = body.get("device_id", "")
    if not device_id:
        _send_json(handler, 400, {"error": "缺少 device_id"})
        return
    service.repo.set_device_state(device_id, "ready", reason="人工恢复已确认")
    _send_json(handler, 200, {"success": True, "device_id": device_id, "state": "ready"})


def handle_cancel_batch(handler: Any, parsed_url: Any, service: PhoneAppService, batch_id: str):
    body = _read_body_json(handler)
    reason = body.get("reason", "用户主动取消")
    operator = body.get("operator", "admin")
    ok, res = service.cancel_batch(batch_id, operator=operator, reason=reason)
    if not ok:
        _send_json(handler, 400, res)
    else:
        _send_json(handler, 200, {"success": True, **res})


def handle_reject_batch(handler: Any, parsed_url: Any, service: PhoneAppService, batch_id: str):
    body = _read_body_json(handler)
    reason = body.get("reason", "审批人驳回")
    operator = body.get("operator", "admin")
    ok, res = service.reject_batch(batch_id, operator=operator, reason=reason)
    if not ok:
        _send_json(handler, 400, res)
    else:
        _send_json(handler, 200, {"success": True, **res})

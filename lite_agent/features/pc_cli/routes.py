"""
PC CLI HTTP REST 路由处理器 (features.pc_cli.routes).
复用宿主 RouteRegistry 与 BaseHTTPRequestHandler 鉴权体系。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict
from urllib.parse import parse_qs

from lite_agent.features.pc_cli.hub import PcCliHub
from lite_agent.features.pc_cli.manager import PcCliManager
from lite_agent.features.pc_cli.stats import collect_dashboard_stats

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2MB


def _send_json(handler: Any, status_code: int, data: Dict[str, Any]) -> None:
    handler.send_response(status_code)
    if hasattr(handler, "_send_cors_headers"):
        handler._send_cors_headers()
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.end_headers()
    handler.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _read_body_json(handler: Any) -> Dict[str, Any]:
    raw_len = handler.headers.get("Content-Length", "0")
    content_len = int(raw_len) if str(raw_len).isdigit() else 0
    if content_len <= 0:
        return {}
    if content_len > MAX_BODY_BYTES:
        _send_json(handler, 413, {"error": f"请求体过大 ({content_len} > {MAX_BODY_BYTES})"})
        raise ValueError("Payload too large")
    body = handler.rfile.read(content_len)
    return json.loads(body.decode("utf-8"))


def _strip_agent_prefix(path: str) -> str:
    if path.startswith("/agent/"):
        return path[6:]
    return path


def handle_get_nodes(handler: Any, parsed_url: Any, hub: PcCliHub) -> None:
    _send_json(handler, 200, {"success": True, "nodes": hub.list_nodes()})


def handle_nodes_sub_dispatch(handler: Any, parsed_url: Any, hub: PcCliHub) -> None:
    path = _strip_agent_prefix(parsed_url.path)
    m = re.match(r"^/api/v1/pc-cli/nodes/([^/]+)/dirs$", path)
    if not m or handler.command.upper() != "GET":
        _send_json(handler, 404, {"error": "Not found"})
        return
    node_id = m.group(1)
    qs = parse_qs(parsed_url.query)
    req_path = qs.get("path", [""])[0]
    ok, res = hub.list_node_dirs(node_id=node_id, path=req_path)
    _send_json(handler, 200 if ok else 400, {"success": ok, **res})


def handle_get_bots(handler: Any, parsed_url: Any, hub: PcCliHub) -> None:
    qs = parse_qs(parsed_url.query)
    include_archived = qs.get("include_archived", ["0"])[0] in ("1", "true", "True")
    _send_json(handler, 200, {"success": True, "bots": hub.list_bots(include_archived=include_archived)})


def handle_post_bots(handler: Any, parsed_url: Any, hub: PcCliHub) -> None:
    try:
        body = _read_body_json(handler)
    except ValueError:
        return
    except Exception as e:
        _send_json(handler, 400, {"error": f"无效 JSON: {e}"})
        return
    ok, res = hub.create_bot(
        name=str(body.get("name") or ""),
        node_id=str(body.get("node_id") or ""),
        engine=str(body.get("engine") or ""),
        cwd=str(body.get("cwd") or ""),
        approval_mode=str(body.get("approval_mode") or "auto"),
        execution_mode=str(body.get("execution_mode") or "agent"),
        reasoning_effort=str(body.get("reasoning_effort") or ""),
        model=body.get("model"),
    )
    _send_json(handler, 201 if ok else 400, {"success": ok, **res})


def handle_bots_sub_dispatch(handler: Any, parsed_url: Any, hub: PcCliHub) -> None:
    path = _strip_agent_prefix(parsed_url.path)
    method = handler.command.upper()
    m = re.match(r"^/api/v1/pc-cli/bots/([^/]+)(?:/(events|turns|cancel))?$", path)
    if not m:
        _send_json(handler, 404, {"error": "Not found"})
        return
    bot_id, action = m.group(1), m.group(2)

    if action is None:
        if method == "GET":
            bot = hub.repo.get_bot(bot_id)
            if not bot:
                _send_json(handler, 404, {"error": "Bot 不存在"})
            else:
                _send_json(handler, 200, {"success": True, "bot": bot})
        elif method == "PATCH":
            try:
                body = _read_body_json(handler)
            except ValueError:
                return
            except Exception as e:
                _send_json(handler, 400, {"error": f"无效 JSON: {e}"})
                return
            ok, res = hub.update_bot(
                bot_id,
                name=body.get("name"),
                approval_mode=body.get("approval_mode"),
                execution_mode=body.get("execution_mode"),
                reasoning_effort=body.get("reasoning_effort"),
                model=body.get("model"),
                archived=body.get("archived"),
                reset_session=bool(body.get("reset_session")),
            )
            _send_json(handler, 200 if ok else 400, {"success": ok, **res})
        elif method == "DELETE":
            ok, res = hub.delete_bot(bot_id)
            _send_json(handler, 200 if ok else 404, {"success": ok, **res})
        else:
            _send_json(handler, 405, {"error": "Method not allowed"})
        return

    if action == "events" and method == "GET":
        qs = parse_qs(parsed_url.query)
        after_seq = int(qs.get("after_seq", ["0"])[0] or "0")
        limit = int(qs.get("limit", ["200"])[0] or "200")
        events = hub.repo.list_events(bot_id=bot_id, after_seq=after_seq, limit=limit)
        _send_json(handler, 200, {"success": True, "bot_id": bot_id, "events": events})
        return

    if action == "turns" and method == "POST":
        try:
            body = _read_body_json(handler)
        except ValueError:
            return
        except Exception as e:
            _send_json(handler, 400, {"error": f"无效 JSON: {e}"})
            return
        ok, res = hub.start_turn(
            bot_id=bot_id,
            prompt=str(body.get("prompt") or ""),
            input_source=str(body.get("input_source") or "text"),
        )
        _send_json(handler, 201 if ok else 400, {"success": ok, **res})
        return

    if action == "cancel" and method == "POST":
        ok, res = hub.cancel_turn(bot_id)
        _send_json(handler, 200 if ok else 400, {"success": ok, **res})
        return

    _send_json(handler, 405, {"error": "Method not allowed"})


def handle_get_approvals(handler: Any, parsed_url: Any, hub: PcCliHub) -> None:
    qs = parse_qs(parsed_url.query)
    bot_id = qs.get("bot_id", [None])[0]
    items = hub.repo.list_pending_approvals(bot_id=bot_id)
    _send_json(handler, 200, {"success": True, "approvals": items})


def handle_approvals_sub_dispatch(handler: Any, parsed_url: Any, hub: PcCliHub) -> None:
    path = _strip_agent_prefix(parsed_url.path)
    m = re.match(r"^/api/v1/pc-cli/approvals/([^/]+)$", path)
    if not m or handler.command.upper() != "POST":
        _send_json(handler, 404, {"error": "Not found"})
        return
    approval_id = m.group(1)
    try:
        body = _read_body_json(handler)
    except ValueError:
        return
    except Exception as e:
        _send_json(handler, 400, {"error": f"无效 JSON: {e}"})
        return
    ok, res = hub.resolve_approval(
        approval_id=approval_id,
        decision=str(body.get("decision") or ""),
        input_source=str(body.get("input_source") or "button"),
    )
    _send_json(handler, 200 if ok else 400, {"success": ok, **res})


def handle_get_models(handler: Any, parsed_url: Any, manager: PcCliManager) -> None:
    _send_json(handler, 200, {"success": True, **manager.list_models()})


def handle_get_stats(handler: Any, parsed_url: Any, hub: PcCliHub) -> None:
    qs = parse_qs(parsed_url.query)
    days = int(qs.get("days", ["7"])[0] or "7")
    _send_json(handler, 200, {"success": True, "stats": collect_dashboard_stats(hub, days=days)})


def handle_post_manager_chat(handler: Any, parsed_url: Any, manager: PcCliManager) -> None:
    try:
        body = _read_body_json(handler)
    except ValueError:
        return
    except Exception as e:
        _send_json(handler, 400, {"error": f"无效 JSON: {e}"})
        return
    ok, res = manager.chat(
        message=str(body.get("message") or ""),
        model_name=str(body.get("model_name") or ""),
        history=body.get("history") if isinstance(body.get("history"), list) else None,
    )
    _send_json(handler, 200 if ok else 400, {"success": ok, **res})

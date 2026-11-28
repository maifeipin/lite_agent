"""
个人知识库 / 私有资产 REST 路由处理器 (features.content_ops.kb_routes).
统一承接 /api/v1/kb/notes*（后续 ebooks / music 等个人资产检索与同步均在此平级扩展），
强制执行所有者鉴权与 Fail-Closed 隐私规则。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict
from urllib.parse import parse_qs

from lite_agent.features.content_ops.hedgedoc_notes import HedgeDocNotesService

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1MB


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
        _send_json(handler, 413, {"success": False, "error": f"请求体过大 ({content_len} > {MAX_BODY_BYTES})"})
        raise ValueError("Payload too large")
    body = handler.rfile.read(content_len)
    return json.loads(body.decode("utf-8"))


def _strip_agent_prefix(path: str) -> str:
    if path.startswith("/agent/"):
        return path[6:]
    return path


def require_kb_owner(handler: Any, notes: HedgeDocNotesService) -> bool:
    """个人知识库私有资产严格限所有者访问：拒绝 guest / edge 及非所有者会话。"""
    if getattr(handler, "is_guest", False) or getattr(handler, "is_edge", False):
        _send_json(handler, 403, {"success": False, "error": "仅知识库所有者可访问私有笔记"})
        return False

    user_session = getattr(handler, "user_session", None)
    if isinstance(user_session, dict):
        username = str(user_session.get("username") or "").strip()
        if not username or username not in notes.allowed_owners:
            _send_json(handler, 403, {"success": False, "error": "仅知识库所有者可访问私有笔记"})
            return False
    return True


def handle_get_notes(
    handler: Any,
    parsed_url: Any,
    notes: HedgeDocNotesService,
) -> None:
    if not require_kb_owner(handler, notes):
        return
    qs = parse_qs(parsed_url.query)
    q = qs.get("q", [""])[0]
    purpose = qs.get("purpose", ["search"])[0] or "search"
    folder_path = qs.get("folder_path", [None])[0] or None
    try:
        limit = int(qs.get("limit", ["20"])[0] or "20")
    except Exception:
        limit = 20
    res = notes.search_notes(query=q, limit=limit, purpose=purpose, folder_path=folder_path)
    _send_json(handler, 200, {"success": True, **res})


def handle_notes_sub_dispatch(
    handler: Any,
    parsed_url: Any,
    notes: HedgeDocNotesService,
) -> None:
    if not require_kb_owner(handler, notes):
        return
    path = _strip_agent_prefix(parsed_url.path)
    method = handler.command.upper()

    if path == "/api/v1/kb/notes/preflight" and method == "GET":
        _send_json(handler, 200, {"success": True, **notes.preflight()})
        return

    if path == "/api/v1/kb/notes/rules" and method == "GET":
        _send_json(handler, 200, {"success": True, "rules": notes.list_rules()})
        return

    if path == "/api/v1/kb/notes/sync" and method == "POST":
        try:
            body = _read_body_json(handler)
        except ValueError:
            return
        except Exception as e:
            _send_json(handler, 400, {"success": False, "error": f"无效 JSON: {e}"})
            return
        raw_ids = body.get("note_ids")
        note_ids = None
        if raw_ids is not None:
            if not isinstance(raw_ids, list):
                _send_json(handler, 400, {"success": False, "error": "note_ids 必须为整数数组"})
                return
            try:
                note_ids = [int(x) for x in raw_ids]
            except Exception:
                _send_json(handler, 400, {"success": False, "error": "note_ids 包含无效整数"})
                return
        ok, res = notes.sync_notes(note_ids=note_ids)
        status = 200 if ok else (412 if res.get("preflight_ok") is False else 503)
        _send_json(handler, status, {"success": ok, **res})
        return

    m_quote = re.match(r"^/api/v1/kb/notes/(\d+)/quote$", path)
    if m_quote and method == "GET":
        note_id = int(m_quote.group(1))
        ok, res = notes.get_note_for_quote(note_id)
        if ok:
            _send_json(handler, 200, {"success": True, **res})
        else:
            code = str(res.get("code") or "")
            status = 403 if code in ("NOTE_EXCLUDED_FROM_INDEX", "NOTE_EXCLUDED_FROM_QUOTE") else 404
            _send_json(handler, status, {"success": False, **res})
        return

    m = re.match(r"^/api/v1/kb/notes/(\d+)/rules$", path)
    if m and method in ("PUT", "POST", "PATCH"):
        note_id = int(m.group(1))
        try:
            body = _read_body_json(handler)
        except ValueError:
            return
        except Exception as e:
            _send_json(handler, 400, {"success": False, "error": f"无效 JSON: {e}"})
            return
        raw_no_idx = body.get("exclude_from_index")
        raw_no_quote = body.get("exclude_from_quote")
        if raw_no_idx is not None and not isinstance(raw_no_idx, bool):
            _send_json(handler, 400, {"success": False, "error": "exclude_from_index 必须为布尔值"})
            return
        if raw_no_quote is not None and not isinstance(raw_no_quote, bool):
            _send_json(handler, 400, {"success": False, "error": "exclude_from_quote 必须为布尔值"})
            return
        ok, res = notes.set_note_rules(
            note_id=note_id,
            exclude_from_index=raw_no_idx,
            exclude_from_quote=raw_no_quote,
        )
        _send_json(handler, 200 if ok else 400, {"success": ok, **res})
        return

    _send_json(handler, 404, {"error": "Not found"})

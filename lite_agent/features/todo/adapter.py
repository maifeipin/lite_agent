"""
Todo HTTP 业务 Adapter 模块 (features.todo.adapter).
负责待办事项在 HTTP 通道上的参数解析、服务调用与标准 JSON 响应封装，
完全解耦公共宿主，宿主仅做传输与认证。
"""

import json
from urllib.parse import parse_qs
from typing import Any
from lite_agent.features.todo.service import TodoService


def get_todo_service(http_handler: Any) -> TodoService:
    """按宿主/上下文配置解析并创建 TodoService 实例。"""
    srv = getattr(http_handler, "server", None)
    api_srv = getattr(srv, "api_server", None)
    app = getattr(api_srv, "app", None)
    agent = getattr(api_srv, "agent", None) if api_srv else None
    db_path = None
    if app and hasattr(app, "todo_db_path"):
        db_path = app.todo_db_path
    elif agent:
        cfg = getattr(agent, "config", {}) or getattr(agent, "_config", {})
        if isinstance(cfg, dict):
            todo_cfg = cfg.get("todo", {})
            if isinstance(todo_cfg, dict):
                db_path = todo_cfg.get("db_path")
            if not db_path:
                db_path = (cfg.get("database", {}) or {}).get("todo_path")
    if not db_path and api_srv and hasattr(api_srv, "config"):
        cfg = api_srv.config
        if isinstance(cfg, dict):
            db_path = (cfg.get("todo", {}) or {}).get("db_path") or (cfg.get("database", {}) or {}).get("todo_path")
    return TodoService(db_path=db_path)


def handle_todos_get(http_handler: Any, parsed_url: Any) -> None:
    qs = parse_qs(parsed_url.query)
    status = qs.get("status", ["pending,active"])[0]
    try:
        todos = get_todo_service(http_handler).get_json(status=status)
        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"success": True, "data": todos}).encode('utf-8'))
    except Exception as e:
        http_handler.send_response(500)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))


def handle_todos_post(http_handler: Any, parsed_url: Any) -> None:
    req_path = parsed_url.path
    norm_path = req_path[6:] if req_path.startswith('/agent/') else req_path
    if norm_path.endswith("/brief/push") or norm_path.endswith("/push-brief"):
        handle_todo_brief_push(http_handler, parsed_url)
        return

    content_length = int(http_handler.headers.get('Content-Length', 0))
    if content_length == 0:
        http_handler.send_error(400, "Bad Request: Empty body")
        return
    body = http_handler.rfile.read(content_length)
    try:
        data = json.loads(body.decode('utf-8'))
        if 'notify_enabled' in data and not isinstance(data['notify_enabled'], bool):
            http_handler.send_error(400, 'notify_enabled must be boolean')
            return
        title = data.get("title")
        if not title:
            http_handler.send_error(400, "Bad Request: Missing title")
            return
        kind = data.get("kind", "misc")
        project = data.get("project")
        description = data.get("description")
        due_at = data.get("due_at")
        remind_interval_mins = data.get("remind_interval_mins", 0)
        remind_before_mins = data.get("remind_before_mins", 30)
        notify_enabled = data.get('notify_enabled', True)
        recur_cron = data.get("recur_cron")
        if recur_cron and kind == "misc":
            kind = "recurring"

        msg = get_todo_service(http_handler).add(
            title=title, kind=kind, project=project, description=description,
            due_at=due_at, recur_cron=recur_cron, remind_interval_mins=remind_interval_mins,
            remind_before_mins=remind_before_mins, notify_enabled=notify_enabled
        )

        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"success": True, "message": msg}).encode('utf-8'))
    except Exception as e:
        http_handler.send_response(500)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))


def handle_todo_brief_push(http_handler: Any, parsed_url: Any) -> None:
    try:
        msg = get_todo_service(http_handler).push_brief()
        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"success": True, "message": msg}).encode('utf-8'))
    except Exception as e:
        http_handler.send_response(500)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))


def handle_todos_patch(http_handler: Any, parsed_url: Any) -> None:
    path = parsed_url.path
    tid = path.split('/')[-1]
    content_length = int(http_handler.headers.get('Content-Length', 0))
    if content_length == 0:
        http_handler.send_error(400, "Bad Request: Empty body")
        return
    body = http_handler.rfile.read(content_length)
    try:
        data = json.loads(body.decode('utf-8'))
        if 'notify_enabled' in data and not isinstance(data['notify_enabled'], bool):
            http_handler.send_error(400, 'notify_enabled must be boolean')
            return
        msg = []
        svc = get_todo_service(http_handler)

        status = data.get("status")
        if status:
            if status == 'done':
                msg.append(svc.done(tid))
            elif status == 'active':
                msg.append(svc.start(tid))
            elif status == 'pending':
                msg.append(svc.resume(tid))
            else:
                http_handler.send_error(400, f"Unsupported status: {status}")
                return

        title = data.get("title")
        description = data.get("description")
        due_at = data.get("due_at")
        project = data.get("project")
        recur_cron = data.get("recur_cron")
        kind = data.get("kind")
        remind_interval_mins = data.get("remind_interval_mins")
        remind_before_mins = data.get("remind_before_mins")
        notify_enabled = data.get('notify_enabled')

        if any(x is not None for x in [title, description, due_at, project, recur_cron, kind, remind_interval_mins, remind_before_mins, notify_enabled]):
            msg.append(svc.update(tid, title=title, description=description, due_at=due_at, project=project, recur_cron=recur_cron, kind=kind, remind_interval_mins=remind_interval_mins, remind_before_mins=remind_before_mins, notify_enabled=notify_enabled))

        if not msg:
            http_handler.send_error(400, "Bad Request: No fields to update")
            return

        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"success": True, "message": " | ".join(msg)}).encode('utf-8'))
    except Exception as e:
        http_handler.send_response(500)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))


def handle_todos_delete(http_handler: Any, parsed_url: Any) -> None:
    path = parsed_url.path
    tid = path.split('/')[-1]
    try:
        deleted = get_todo_service(http_handler).delete(tid)

        if deleted:
            msg = f"✅ 任务 {tid} 已被永久删除"
        else:
            http_handler.send_response(404)
            if hasattr(http_handler, "_send_cors_headers"):
                http_handler._send_cors_headers()
            http_handler.send_header('Content-Type', 'application/json')
            http_handler.end_headers()
            http_handler.wfile.write(json.dumps({"success": False, "detail": f"Todo {tid} not found"}).encode('utf-8'))
            return

        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"success": True, "message": msg}).encode('utf-8'))
    except Exception as e:
        http_handler.send_response(500)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))

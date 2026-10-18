"""
Todo feature domain (features.todo).
管理待办事项全生命周期、周期提醒与数据仓储。
实现受控插件 register(context) 契约。
"""

from typing import Any, Optional
from lite_agent.features.todo.service import TodoService

TODO_DOMAIN_SPEC = {
    "pattern": r"(待办|todo|提醒|延期|搁置)",
    "default_tools": [
        "todo_list", "todo_get",
    ],
    "explicit_intent_tools": [
        "todo_push_brief",
        "todo_add", "todo_start", "todo_done", "todo_drop",
        "todo_snooze", "todo_shelve", "todo_resume",
        "todo_update", "todo_dispatch",
    ],
    "explicit_patterns": r"(添加|新建|标记.*完成|标记.*延期|搁置|恢复|更新|派发|删除|放弃|推送)",
    "explicit_routes": [
        (r"标记.*完成|完成.*待办", ["todo_done"]),
        (r"删除|放弃", ["todo_drop"]),
        (r"推送|简报", ["todo_push_brief"]),
        (r"添加|新建", ["todo_add"]),
        (r"延期|搁置", ["todo_snooze", "todo_shelve"]),
    ],
}


def register(context: Any) -> None:
    """注册 Todo 插件到宿主上下文。"""
    from lite_agent.skills import ops_todo
    from lite_agent.core.skill_engine import _skill_registry

    cfg = getattr(context, "config", {}) or {}
    todo_cfg = cfg.get("todo", {}) if isinstance(cfg, dict) else {}
    configured_db_path = todo_cfg.get("db_path") if isinstance(todo_cfg, dict) else None
    if not configured_db_path:
        db_cfg = cfg.get("database", {}) if isinstance(cfg, dict) else {}
        configured_db_path = db_cfg.get("todo_path") if isinstance(db_cfg, dict) else None

    # 1. 注册待办工具技能，绑定实例级数据库配置
    todo_tools = [
        "todo_add", "todo_list", "todo_get", "todo_start", "todo_done",
        "todo_drop", "todo_snooze", "todo_shelve", "todo_resume",
        "todo_update", "todo_dispatch", "todo_push_brief"
    ]
    if any(t not in _skill_registry for t in todo_tools):
        import importlib
        importlib.reload(ops_todo)

    for t_name in todo_tools:
        if t_name in _skill_registry:
            tool_meta = _skill_registry[t_name]
            if configured_db_path:
                orig_fn = tool_meta["func"]
                def _make_bound_tool_fn(fn, db_p):
                    def _bound_fn(*args, **kwargs):
                        if "db_path" not in kwargs:
                            kwargs["db_path"] = db_p
                        return fn(*args, **kwargs)
                    return _bound_fn
                bound_meta = dict(tool_meta)
                bound_meta["func"] = _make_bound_tool_fn(orig_fn, configured_db_path)
                context.register_skill(t_name, bound_meta)
            else:
                context.register_skill(t_name, tool_meta)

    # 2. 注册待办 HTTP 业务路由，直接派发至 adapter
    from lite_agent.features.todo import adapter

    def _handle_todos_route(http_handler: Any, parsed_url: Any):
        method = http_handler.command
        req_path = parsed_url.path
        norm_path = req_path[6:] if req_path.startswith('/agent/') else req_path
        if method == "GET":
            adapter.handle_todos_get(http_handler, parsed_url)
        elif method == "POST":
            if norm_path.endswith("/brief/push") or norm_path.endswith("/push-brief"):
                if hasattr(http_handler, "_handle_post_todo_brief_push"):
                    http_handler._handle_post_todo_brief_push()
                else:
                    adapter.handle_todo_brief_push(http_handler, parsed_url)
            else:
                if hasattr(http_handler, "_handle_post_todo"):
                    http_handler._handle_post_todo()
                else:
                    adapter.handle_todos_post(http_handler, parsed_url)
        elif method == "PATCH":
            adapter.handle_todos_patch(http_handler, parsed_url)
        elif method == "DELETE":
            adapter.handle_todos_delete(http_handler, parsed_url)
        else:
            http_handler.send_error(405, "Method Not Allowed")

    context.register_route("ALL", "/api/v1/todos", _handle_todos_route, is_prefix=True)

    # 3. 注册定时作业，绑定实例级数据库配置
    cron_tick_fn = (lambda: ops_todo._todo_tick(db_path=configured_db_path)) if configured_db_path else ops_todo._todo_tick
    cron_brief_fn = (lambda: ops_todo.todo_push_brief(db_path=configured_db_path)) if configured_db_path else ops_todo.todo_push_brief
    context.register_cron("todo_tick", "every_minute", cron_tick_fn)
    context.register_cron("todo_brief", "08:30", cron_brief_fn)

    # 4. 注册待办 RequestSelector 领域路由
    if hasattr(context, "register_domain"):
        context.register_domain("todo", TODO_DOMAIN_SPEC)


__all__ = ["TodoService", "register", "TODO_DOMAIN_SPEC"]

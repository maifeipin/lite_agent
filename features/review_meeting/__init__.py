"""
Review Meeting 决策评审会审模块 (features.review_meeting).
管理评审会议室、授权出箱、议题状态机与会审通道。
实现受控插件 register(context) 契约。
"""

from typing import Any
from contracts.message import AgentResponse

REVIEW_MEETING_DOMAIN_SPEC = {
    "pattern": r"(会审|评审会议|评审会|review[_ -]?meeting)",
    "default_tools": ["ops_review_meeting_status", "ops_review_meeting_show"],
    "explicit_intent_tools": [
        "ops_review_meeting_join", "ops_review_meeting_leave",
        "ops_review_meeting_submit", "ops_review_meeting_comment",
    ],
    "explicit_patterns": r"(入会|加入|#invite=|离会|退出|提交|发言|评论|回复|补充)",
    "explicit_routes": [
        (r"入会|加入|#invite=", ["ops_review_meeting_join"]),
        (r"离会|退出", ["ops_review_meeting_leave"]),
        (r"提交.*(?:意见|评审)|(?<!补充)发言", ["ops_review_meeting_submit"]),
        (r"评论|回复|补充", ["ops_review_meeting_comment"]),
    ],
}

DECISION_DOMAIN_SPEC = {
    "pattern": r"(多模型评判|评判委员会|委员会评估|委员会决策)",
    "default_tools": [],
    "explicit_intent_tools": ["ops_decision"],
    "explicit_patterns": r"(多模型评判|评判委员会|委员会评估|委员会决策)",
}


def _handle_meeting_cmd(agent: Any, msg: Any, args: list) -> AgentResponse:
    from features.review_meeting.channel_commands import handle as meeting_command
    import sqlite3
    try:
        meeting_reply = meeting_command(agent._config, msg)
    except (ValueError, OSError) as exc:
        meeting_reply = f'会议操作未完成：{exc}'
    except sqlite3.Error:
        meeting_reply = '会议数据库暂不可用，请稍后重试；本次操作未确认成功。'
    except Exception as exc:
        meeting_reply = f'会议服务暂不可用：{exc}'
    if meeting_reply is not None:
        return AgentResponse(meeting_reply, title='会议操作', color='blue')
    return AgentResponse("未知会议操作", title='会议操作', color='grey')


def register(context: Any) -> None:
    """注册 Review Meeting 插件到宿主上下文。"""
    from skills import ops_review_meeting, ops_decision
    from core.skill_engine import _skill_registry

    # 1. 注册会审与决策技能
    meeting_tools = [
        "ops_review_meeting_status", "ops_review_meeting_show",
        "ops_review_meeting_join", "ops_review_meeting_leave",
        "ops_review_meeting_submit", "ops_review_meeting_comment",
        "ops_decision"
    ]
    for t_name in meeting_tools:
        if t_name in _skill_registry:
            context.register_skill(t_name, _skill_registry[t_name])




    # 2. 注册 /meeting 指令
    context.register_command(
        "/meeting",
        _handle_meeting_cmd,
        category="会议",
        description="决策评审会审会议室操作指令",
        admin_only=True,
    )

    # 3. 注册路由
    from features.review_meeting import http_adapter

    def _handle_meeting_route(http_handler: Any, parsed_url: Any):
        method = http_handler.command
        req_path = parsed_url.path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'

        if req_path.startswith('/api/v1/review-meetings'):
            http_adapter.handle_review_meeting_api(http_handler, method, req_path, parsed_url.query)
        elif req_path == '/api/v1/auth':
            if hasattr(http_handler, "_handle_auth"):
                http_handler._handle_auth()
            else:
                http_handler.send_error(404, "Not Found")
        else:
            http_handler.send_error(404, "Not Found")

    context.register_route(
        "ALL",
        "/api/v1/review-meetings",
        _handle_meeting_route,
        is_prefix=True,
        auth_required=False,
        cors_restricted=True,
    )

    def _handle_meeting_auth_route(http_handler: Any, parsed_url: Any):
        http_adapter.handle_auth(http_handler)

    context.register_route(
        "POST",
        "/api/v1/auth",
        _handle_meeting_auth_route,
        auth_required=False,
        cors_restricted=True,
    )

    # 4. 注册会审通知监听器 (如果配置启用)
    cfg = getattr(context, "config", {}) or {}
    notif_cfg = cfg.get("review_meeting_notifications", {})
    if notif_cfg.get("enabled"):
        from features.review_meeting.notifications import start_notifier
        agent = getattr(context, "agent", None)
        stop_fn = start_notifier(agent, cfg)
        if stop_fn:
            if hasattr(context, "record_cleanup"):
                context.record_cleanup(stop_fn, "review_meeting_notifications")
            elif hasattr(context, "register_cleanup"):
                context.register_cleanup("review_meeting_notifications", stop_fn)

    # 5. 注册会审与决策 RequestSelector 领域路由
    if hasattr(context, "register_domain"):
        context.register_domain("review_meeting", REVIEW_MEETING_DOMAIN_SPEC)
        context.register_domain("decision", DECISION_DOMAIN_SPEC)


__all__ = ["register", "REVIEW_MEETING_DOMAIN_SPEC", "DECISION_DOMAIN_SPEC"]

"""lite_agent skills for reading and participating in a review meeting.

Thin entry point delegating to features.review_meeting.invitation_service.
Administrative actions, especially the human-only `decide`, remain in the CLI.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lite_agent.core.skill_engine import skill
from lite_agent.features.review_meeting import room
from lite_agent.features.review_meeting import invitation_service

# 兼容符号与辅助函数导出
invitation_parts = invitation_service.invitation_parts
claim_invitation = invitation_service.claim_invitation
_db = invitation_service.get_db
_write_action = invitation_service.write_action
_attendance_action = invitation_service.attendance_action


@skill(
    name="ops_review_meeting_status",
    description="查询共享会审室状态、轮次、参会名单和缺席者；只读。",
    params={"meeting_id": {"type": "string", "description": "议题 ID"}},
    tags=["review", "committee"], side_effect=False,
)
def ops_review_meeting_status(meeting_id: str) -> str:
    return invitation_service.get_meeting_status(meeting_id)


@skill(
    name="ops_review_meeting_show",
    description="增量读取共享会审室的原始提案与逐条发言。首次 since=0；以后传上次最大 seq，节省 token。",
    params={
        "meeting_id": {"type": "string", "description": "议题 ID"},
        "since": {"type": "integer", "description": "只返回此序号之后的事件", "default": 0},
    },
    tags=["review", "committee"], side_effect=False,
)
def ops_review_meeting_show(meeting_id: str, since: int = 0) -> str:
    return invitation_service.get_meeting_show(meeting_id, since=since)


@skill(
    name="ops_review_meeting_join",
    description="领取一次性邀请并入会：传完整 invite_url 或 invite_token。返回实际 participant，后续提交意见必须使用该名称。仅已有名单的旧会议可只传 participant。入会不算提交意见。",
    params={"meeting_id": {"type": "string", "description": "议题 ID"},
            "participant": {"type": "string", "description": "旧会议已登记名称；新邀请的名称由服务端决定", "default": "agent"},
            "invite_url": {"type": "string", "description": "包含 #invite= 的完整邀请链接", "default": ""},
            "invite_token": {"type": "string", "description": "邀请口令；与 invite_url 二选一", "default": ""}},
    tags=["review", "committee"], side_effect=True,
)
def ops_review_meeting_join(meeting_id: str, participant: str = "agent",
                            invite_url: str = "", invite_token: str = "") -> str:
    return invitation_service.join_meeting(
        meeting_id, participant=participant, invite_url=invite_url, invite_token=invite_token
    )


@skill(
    name="ops_review_meeting_leave",
    description="登记离会；保留已提交的意见，未提交者仍需主持人注明缺席。",
    params={"meeting_id": {"type": "string", "description": "议题 ID"},
            "participant": {"type": "string", "description": "已获邀的参会名称"}},
    tags=["review", "committee"], side_effect=True,
)
def ops_review_meeting_leave(meeting_id: str, participant: str) -> str:
    return invitation_service.leave_meeting(meeting_id, participant=participant)


@skill(
    name="ops_review_meeting_submit",
    description="以已获邀参会名称提交本轮一次正式意见；第二轮起须回应另一评委此前发言的 seq。不得代用户审批。",
    params={
        "meeting_id": {"type": "string", "description": "议题 ID"},
        "participant": {"type": "string", "description": "已获邀的参会名称，如 cursor、qwen、workbuddy"},
        "position": {"type": "string", "description": "立场", "enum": list(room.POSITIONS)},
        "review": {"type": "string", "description": "意见正文，含事实、分歧、依据和建议"},
        "responds_to_seq": {"type": "integer", "description": "第二轮起必填；此前另一评委发言序号", "default": 0},
    },
    tags=["review", "committee"], side_effect=True,
)
def ops_review_meeting_submit(meeting_id: str, participant: str, position: str,
                              review: str, responds_to_seq: int = 0) -> str:
    return invitation_service.submit_review(
        meeting_id, participant=participant, position=position,
        review=review, responds_to_seq=responds_to_seq
    )


@skill(
    name="ops_review_meeting_comment",
    description="在开放会议中追加讨论留言，可回应任意已有事件序号；不会覆盖正式意见。",
    params={
        "meeting_id": {"type": "string", "description": "议题 ID"},
        "participant": {"type": "string", "description": "已获邀的参会名称"},
        "comment": {"type": "string", "description": "讨论内容"},
        "responds_to_seq": {"type": "integer", "description": "被回应的事件序号；0 表示不指定", "default": 0},
    },
    tags=["review", "committee"], side_effect=True,
)
def ops_review_meeting_comment(meeting_id: str, participant: str, comment: str,
                               responds_to_seq: int = 0) -> str:
    return invitation_service.add_comment(
        meeting_id, participant=participant, comment=comment, responds_to_seq=responds_to_seq
    )

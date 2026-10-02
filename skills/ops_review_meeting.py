"""lite_agent skills for reading and participating in a review meeting.

Administrative actions, especially the human-only `decide`, remain in the CLI.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from core.skill_engine import skill
from scripts import review_meeting as room


def _db():
    return room.connect(Path(os.environ.get("REVIEW_MEETING_DB", room.DEFAULT_DB)))


def _write_action(action: str, *, text: str, **kwargs) -> str:
    with tempfile.TemporaryDirectory(prefix="review_meeting_") as directory:
        file = Path(directory) / "entry.md"
        file.write_text(text, encoding="utf-8")
        args = SimpleNamespace(action=action, file=str(file), **kwargs)
        with closing(_db()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                result = room.execute(db, args)
        return json.dumps(result, ensure_ascii=False)


def _attendance_action(action: str, meeting_id: str, participant: str) -> str:
    args = SimpleNamespace(action=action, id=meeting_id, participant=participant)
    with closing(_db()) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            result = room.execute(db, args)
    return json.dumps(result, ensure_ascii=False)


@skill(
    name="ops_review_meeting_status",
    description="查询共享会审室状态、轮次、参会名单和缺席者；只读。",
    params={"meeting_id": {"type": "string", "description": "议题 ID"}},
    tags=["review", "committee"], side_effect=False,
)
def ops_review_meeting_status(meeting_id: str) -> str:
    with closing(_db()) as db:
        return json.dumps(room.snapshot(db, meeting_id), ensure_ascii=False)


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
    if since < 0:
        raise ValueError("since 不能为负")
    with closing(_db()) as db:
        return json.dumps(room.snapshot(db, meeting_id, since=since, full=True), ensure_ascii=False)


@skill(
    name="ops_review_meeting_join",
    description="以已获邀参会名称登记入会；仅记录出席状态，不算正式意见。",
    params={"meeting_id": {"type": "string", "description": "议题 ID"},
            "participant": {"type": "string", "description": "已获邀的参会名称"}},
    tags=["review", "committee"], side_effect=True,
)
def ops_review_meeting_join(meeting_id: str, participant: str) -> str:
    return _attendance_action("join", meeting_id, participant)


@skill(
    name="ops_review_meeting_leave",
    description="登记离会；保留已提交的意见，未提交者仍需主持人注明缺席。",
    params={"meeting_id": {"type": "string", "description": "议题 ID"},
            "participant": {"type": "string", "description": "已获邀的参会名称"}},
    tags=["review", "committee"], side_effect=True,
)
def ops_review_meeting_leave(meeting_id: str, participant: str) -> str:
    return _attendance_action("leave", meeting_id, participant)


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
    if position not in room.POSITIONS:
        raise ValueError("无效立场")
    return _write_action("submit", text=review, id=meeting_id, participant=participant,
                         position=position, responds_to=responds_to_seq or None)


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
    return _write_action("comment", text=comment, id=meeting_id, participant=participant,
                         responds_to=responds_to_seq or None)

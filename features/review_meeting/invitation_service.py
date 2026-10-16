"""
Review Meeting 会议邀请与参会业务服务 (features.review_meeting.invitation_service)。
从 skills/ops_review_meeting.py 迁入邀请解析、入会领取、参会协作及只读状态查询。
使 channel_commands 与 skills 工具共用 canonical 服务。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Tuple
from urllib.parse import urlsplit, parse_qs

from features.review_meeting import room
from features.review_meeting import api as access


def invitation_parts(invite_url: str) -> Tuple[str, str]:
    """解析邀请链接中的 meeting_id 与 token"""
    match = re.search(r"https://[^\s<>\[\]()]+", invite_url)
    if not match:
        raise ValueError("需提供完整 HTTPS 邀请链接，包含 #invite=")
    url = urlsplit(match.group(0))
    path = re.fullmatch(r"/agent/api/v1/review-meetings/([a-f0-9]{12})/invite", url.path)
    tokens = parse_qs(url.fragment).get("invite", [])
    if not path or len(tokens) != 1:
        raise ValueError("邀请链接格式不正确")
    return path.group(1), tokens[0]


def get_db(db_path: Optional[str] = None):
    return room.connect(Path(db_path or os.environ.get("REVIEW_MEETING_DB", room.DEFAULT_DB)))


def claim_invitation(meeting_id: str, invite_token: str, participant: str = "agent", db_path: Optional[str] = None) -> str:
    """领取邀请并入会"""
    with closing(get_db(db_path)) as db:
        access.ensure_tables(db)
        with db:
            db.execute("BEGIN IMMEDIATE")
            result = access.join(db, meeting_id, invite_token, participant,
                                 metadata={"client": "lite_agent"})
            # These trusted local skills use the shared DB, not HTTP sessions.
            # Do not expose an unused bearer credential to the model/history.
            db.execute("UPDATE meeting_sessions SET revoked_at=? WHERE token_hash=?",
                       (int(time.time()), room.sha(result.pop("session_token"))))
            result.pop("session_expires_at", None)
            result["next_action"] = "使用返回的 participant 复核议题并调用 ops_review_meeting_submit；入会本身不算提交意见。"
            return json.dumps(result, ensure_ascii=False)


def write_action(action: str, *, text: str, db_path: Optional[str] = None, **kwargs) -> str:
    with tempfile.TemporaryDirectory(prefix="review_meeting_") as directory:
        file = Path(directory) / "entry.md"
        file.write_text(text, encoding="utf-8")
        args = SimpleNamespace(action=action, file=str(file), **kwargs)
        with closing(get_db(db_path)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                result = room.execute(db, args)
        return json.dumps(result, ensure_ascii=False)


def attendance_action(action: str, meeting_id: str, participant: str, db_path: Optional[str] = None) -> str:
    args = SimpleNamespace(action=action, id=meeting_id, participant=participant)
    with closing(get_db(db_path)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            result = room.execute(db, args)
    return json.dumps(result, ensure_ascii=False)


def get_meeting_status(meeting_id: str, db_path: Optional[str] = None) -> str:
    """查询共享会审室状态、轮次、参会名单和缺席者；只读。"""
    with closing(get_db(db_path)) as db:
        return json.dumps(room.snapshot(db, meeting_id), ensure_ascii=False)


def get_meeting_show(meeting_id: str, since: int = 0, db_path: Optional[str] = None) -> str:
    """增量读取共享会审室的原始提案与逐条发言。"""
    if since < 0:
        raise ValueError("since 不能为负")
    with closing(get_db(db_path)) as db:
        return json.dumps(room.snapshot(db, meeting_id, since=since, full=True), ensure_ascii=False)


def join_meeting(meeting_id: str, participant: str = "agent",
                 invite_url: str = "", invite_token: str = "", db_path: Optional[str] = None) -> str:
    """领取一次性邀请并入会"""
    if invite_url:
        url_id, token = invitation_parts(invite_url)
        if url_id != meeting_id or (invite_token and invite_token != token):
            raise ValueError("会议 ID 或邀请口令与链接不匹配")
        invite_token = token
    if invite_token:
        return claim_invitation(meeting_id, invite_token, participant, db_path=db_path)
    return attendance_action("join", meeting_id, participant, db_path=db_path)


def leave_meeting(meeting_id: str, participant: str, db_path: Optional[str] = None) -> str:
    """登记离会"""
    return attendance_action("leave", meeting_id, participant, db_path=db_path)


def submit_review(meeting_id: str, participant: str, position: str,
                  review: str, responds_to_seq: int = 0, db_path: Optional[str] = None) -> str:
    """提交评审意见"""
    if position not in room.POSITIONS:
        raise ValueError("无效立场")
    return write_action("submit", text=review, id=meeting_id, participant=participant,
                        position=position, responds_to=responds_to_seq or None, db_path=db_path)


def add_comment(meeting_id: str, participant: str, comment: str,
                responds_to_seq: int = 0, db_path: Optional[str] = None) -> str:
    """追加讨论留言"""
    return write_action("comment", text=comment, id=meeting_id, participant=participant,
                        responds_to=responds_to_seq or None, db_path=db_path)

"""Narrow, short-lived HTTP capabilities for review meetings.

The invitation can only create a meeting seat. Each seat receives its own
temporary token: reads remain available after discussion freezes; writes require open.
"""
from __future__ import annotations

import re
import secrets
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from scripts import review_meeting as room

INVITE_TTL_DEFAULT = 24 * 60 * 60
INVITE_TTL_MIN = 5 * 60
INVITE_TTL_MAX = 7 * 24 * 60 * 60
SESSION_TTL = 24 * 60 * 60
HOST_SESSION_TTL = 3600
MAX_PARTICIPANTS = 32
MAX_TEXT = 20_000


class ReviewAPIError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def ensure_tables(db) -> None:
    db.executescript("""
      CREATE TABLE IF NOT EXISTS meeting_invites (
        meeting_id TEXT PRIMARY KEY REFERENCES meetings(id),
        token_hash TEXT NOT NULL, expires_at INTEGER NOT NULL,
        revoked_at INTEGER
      );
      CREATE TABLE IF NOT EXISTS meeting_sessions (
        token_hash TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id),
        participant TEXT NOT NULL, expires_at INTEGER NOT NULL,
        revoked_at INTEGER,
        FOREIGN KEY(meeting_id,participant) REFERENCES participants(meeting_id,name)
      );
      CREATE TABLE IF NOT EXISTS review_host_sessions (
        token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role='host'), expires_at INTEGER NOT NULL, revoked_at INTEGER
      );
      CREATE INDEX IF NOT EXISTS meeting_sessions_seat
        ON meeting_sessions(meeting_id,participant);
    """)


def issue_host_session(db, username: str, *, now=None) -> dict:
    now = int(time.time()) if now is None else now
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM review_host_sessions WHERE expires_at<=? OR revoked_at IS NOT NULL", (now,))
    db.execute("INSERT INTO review_host_sessions VALUES(?,?,'host',?,NULL)",
               (room.sha(token), username, now + HOST_SESSION_TTL))
    return {"review_token": token, "review_token_expires_at": now + HOST_SESSION_TTL,
            "scope": "review-meetings:host"}


def is_host_session(db, token, *, now=None) -> bool:
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        return False
    now = int(time.time()) if now is None else now
    row = db.execute("SELECT role,expires_at,revoked_at FROM review_host_sessions WHERE token_hash=?",
                     (room.sha(token),)).fetchone()
    return bool(row and row["role"] == "host" and row["expires_at"] > now and row["revoked_at"] is None)


def revoke_host_session(db, token) -> None:
    if not is_host_session(db, token):
        raise ReviewAPIError("主持人会话无效或已过期", 401)
    db.execute("UPDATE review_host_sessions SET revoked_at=? WHERE token_hash=?",
               (int(time.time()), room.sha(token)))


def _ttl(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not INVITE_TTL_MIN <= value <= INVITE_TTL_MAX:
        raise ReviewAPIError("invite_ttl_seconds 必须是 300 到 604800 的整数")
    return value


def _text(value, field: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ReviewAPIError(f"{field} 必须是 1 到 {limit} 字符的非空文本")
    return value.strip()


def _open(db, meeting_id: str):
    try:
        row = room.get(db, meeting_id)
        room.require_open(row)
        return row
    except ValueError as exc:
        raise ReviewAPIError(str(exc), 404 if "未知议题" in str(exc) else 403) from exc


def create(db, *, title: str, brief: str, owner_key_hash: str,
           invite_ttl_seconds: int = INVITE_TTL_DEFAULT, now: int | None = None) -> dict:
    title = _text(title, "title", 200)
    brief = _text(brief, "brief")
    if not isinstance(owner_key_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", owner_key_hash):
        raise ReviewAPIError("owner_key_hash 必须是人类审批密钥的 SHA-256 十六进制值")
    ttl = _ttl(invite_ttl_seconds)
    now = int(time.time()) if now is None else now
    meeting_id = uuid.uuid4().hex[:12]
    db.execute("INSERT INTO meetings VALUES(?,?,?,?,?,?,?,?,?)", (
        meeting_id, title, brief, room.OPEN, 1, owner_key_hash, now, now, None))
    room.append(db, room.get(db, meeting_id), "system", "created", {
        "brief_sha256": room.sha(brief), "participants": []}, created_at=now)
    invitation = issue_invite(db, meeting_id, ttl, now=now)
    return {"id": meeting_id, "title": title, **invitation}


def issue_invite(db, meeting_id: str, ttl: int = INVITE_TTL_DEFAULT,
                 *, now: int | None = None) -> dict:
    row = _open(db, meeting_id)
    ttl = _ttl(ttl)
    now = int(time.time()) if now is None else now
    previous = db.execute("SELECT 1 FROM meeting_invites WHERE meeting_id=?", (meeting_id,)).fetchone()
    if previous:
        db.execute("""UPDATE meeting_sessions SET revoked_at=?
                      WHERE meeting_id=? AND revoked_at IS NULL""", (now, meeting_id))
    token = secrets.token_urlsafe(32)
    expires_at = now + ttl
    db.execute("""INSERT INTO meeting_invites(meeting_id,token_hash,expires_at,revoked_at)
                  VALUES(?,?,?,NULL)
                  ON CONFLICT(meeting_id) DO UPDATE SET
                    token_hash=excluded.token_hash,expires_at=excluded.expires_at,revoked_at=NULL""",
               (meeting_id, room.sha(token), expires_at))
    room.append(db, row, "system", "invite_issued", {
        "expires_at": expires_at, "prior_sessions_revoked": bool(previous)}, created_at=now)
    return {"invite_token": token, "invite_expires_at": expires_at}


def _seat_name(db, meeting_id: str, label: str, now: int) -> tuple[str, bool]:
    if not isinstance(label, str) or len(label) > 100:
        raise ReviewAPIError("label 长度不能超过 100")
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:32] or "agent"
    if len(base) < 2 or not base[0].isalpha() or base in {"owner", "human", "system"} or base.startswith("model-"):
        base = "agent-" + base[:25]
    for number in range(1, MAX_PARTICIPANTS + 2):
        name = base if number == 1 else f"{base[:35]}-{number}"
        existing = db.execute("SELECT 1 FROM participants WHERE meeting_id=? AND name=?", (meeting_id, name)).fetchone()
        if not existing:
            return name, True
        active = db.execute("""SELECT 1 FROM meeting_sessions
                               WHERE meeting_id=? AND participant=? AND expires_at>? AND revoked_at IS NULL""",
                            (meeting_id, name, now)).fetchone()
        if not active:
            return name, False
    raise ReviewAPIError("参会席位已满", 409)


def join(db, meeting_id: str, invite_token: str, label: str = "agent",
         *, now: int | None = None) -> dict:
    row = _open(db, meeting_id)
    now = int(time.time()) if now is None else now
    invitation = db.execute("SELECT * FROM meeting_invites WHERE meeting_id=?", (meeting_id,)).fetchone()
    if (not isinstance(invite_token, str) or len(invite_token) < 32 or len(invite_token) > 128
            or invitation is None or invitation["revoked_at"] is not None
            or invitation["expires_at"] <= now
            or not secrets.compare_digest(room.sha(invite_token), invitation["token_hash"])):
        raise ReviewAPIError("邀请凭据无效或已过期", 401)
    name, new = _seat_name(db, meeting_id, label, now)
    if new:
        count = db.execute("SELECT COUNT(*) FROM participants WHERE meeting_id=?", (meeting_id,)).fetchone()[0]
        if count >= MAX_PARTICIPANTS:
            raise ReviewAPIError("参会席位已满", 409)
        db.execute("INSERT INTO participants VALUES(?,?,?)", (meeting_id, name, row["round"]))
        room.append(db, row, "system", "invited", {"name": name, "from_round": row["round"]}, created_at=now)
    if room.presence(db, meeting_id, row["round"])[name]["state"] != "present":
        room.append(db, row, name, "joined", {}, created_at=now)
    token = secrets.token_urlsafe(32)
    expires_at = now + SESSION_TTL
    db.execute("INSERT INTO meeting_sessions VALUES(?,?,?,?,NULL)",
               (room.sha(token), meeting_id, name, expires_at))
    return {"id": meeting_id, "participant": name, "session_token": token,
            "session_expires_at": expires_at, "meeting": room.snapshot(db, meeting_id, full=True)}


def authenticate(db, meeting_id: str, token: str, *, now: int | None = None) -> str:
    try:
        room.get(db, meeting_id)
    except ValueError as exc:
        raise ReviewAPIError(str(exc), 404) from exc
    now = int(time.time()) if now is None else now
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        raise ReviewAPIError("会话凭据无效或已过期", 401)
    digest = room.sha(token)
    session = db.execute("SELECT * FROM meeting_sessions WHERE token_hash=?", (digest,)).fetchone()
    if (session is None or session["meeting_id"] != meeting_id
            or session["expires_at"] <= now or session["revoked_at"] is not None
            or not secrets.compare_digest(digest, session["token_hash"])):
        raise ReviewAPIError("会话凭据无效或已过期", 401)
    return session["participant"]


def show(db, meeting_id: str, token: str, since: int = 0) -> dict:
    authenticate(db, meeting_id, token)
    if isinstance(since, bool) or not isinstance(since, int) or since < 0:
        raise ReviewAPIError("since 必须为非负整数")
    return room.snapshot(db, meeting_id, since=since, full=True)


def _write(db, action: str, meeting_id: str, token: str, text: str,
           responds_to_seq: int | None = None, position: str | None = None) -> dict:
    actor = authenticate(db, meeting_id, token)
    _open(db, meeting_id)
    text = _text(text, "text")
    if responds_to_seq is not None and (isinstance(responds_to_seq, bool)
                                         or not isinstance(responds_to_seq, int) or responds_to_seq <= 0):
        raise ReviewAPIError("responds_to_seq 必须为正整数")
    if action == "submit" and position not in room.POSITIONS:
        raise ReviewAPIError("position 无效")
    with tempfile.TemporaryDirectory(prefix="review_meeting_api_") as directory:
        path = Path(directory) / "entry.md"
        path.write_text(text, encoding="utf-8")
        args = SimpleNamespace(action=action, id=meeting_id, participant=actor,
                               position=position, responds_to=responds_to_seq, file=str(path))
        return room.execute(db, args)


def review(db, meeting_id: str, token: str, text: str, position: str,
           responds_to_seq: int | None = None) -> dict:
    return _write(db, "submit", meeting_id, token, text, responds_to_seq, position)


def comment(db, meeting_id: str, token: str, text: str,
            responds_to_seq: int | None = None) -> dict:
    return _write(db, "comment", meeting_id, token, text, responds_to_seq)


def leave(db, meeting_id: str, token: str, *, now: int | None = None) -> dict:
    actor = authenticate(db, meeting_id, token, now=now)
    row = room.get(db, meeting_id)
    if row["state"] in ("approved", "rejected") or row["archived_at"]:
        event = room.append(db, row, actor, "left", {})
        result = {"id": meeting_id, "participant": actor, "state": "left",
                  "changed": True, "event_seq": event["seq"]}
    else:
        result = room.execute(db, SimpleNamespace(action="leave", id=meeting_id, participant=actor))
    db.execute("UPDATE meeting_sessions SET revoked_at=? WHERE token_hash=?", (
        int(time.time()) if now is None else now, room.sha(token)))
    return result

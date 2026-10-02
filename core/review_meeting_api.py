"""Narrow, short-lived HTTP capabilities for review meetings.

The invitation can only create a meeting seat. Each seat receives its own
temporary token: reads remain available after discussion freezes; writes require open.
"""
from __future__ import annotations

import re
import json
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
      CREATE TABLE IF NOT EXISTS meeting_seat_grants (
        token_hash TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id),
        participant_id TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
        expires_at INTEGER NOT NULL, claimed_at INTEGER, revoked_at INTEGER,
        participant TEXT, metadata TEXT NOT NULL DEFAULT '{}'
      );
      CREATE TABLE IF NOT EXISTS meeting_cohosts (
        token_hash TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id),
        operator_id TEXT NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER
      );
    """)
    columns = {r['name'] for r in db.execute('PRAGMA table_info(meeting_seat_grants)')}
    if 'recovery_hash' not in columns:
        db.execute('ALTER TABLE meeting_seat_grants ADD COLUMN recovery_hash TEXT')


def recover_seat(db, meeting_id, participant_id, secret):
    row = _open(db, meeting_id)
    if not isinstance(secret, str) or not 32 <= len(secret) <= 128:
        raise ReviewAPIError('恢复凭据无效', 401)
    grant = db.execute('SELECT * FROM meeting_seat_grants WHERE meeting_id=? AND participant_id=?', (meeting_id, participant_id)).fetchone()
    now = int(time.time())
    if (not grant or not grant['claimed_at'] or grant['revoked_at'] is not None
            or grant['claimed_at'] + SESSION_TTL <= now or not grant['recovery_hash']
            or not secrets.compare_digest(room.sha(secret), grant['recovery_hash'])):
        raise ReviewAPIError('恢复凭据无效或席位已撤销', 401)
    recent = db.execute("SELECT COUNT(*) FROM events WHERE meeting_id=? AND actor=? AND kind='session_reissued' AND created_at>?", (meeting_id, grant['participant'], now - 3600)).fetchone()[0]
    if recent >= 5:
        raise ReviewAPIError('恢复请求过多，请联系主持人', 429)
    token = secrets.token_urlsafe(32)
    db.execute('UPDATE meeting_sessions SET revoked_at=? WHERE meeting_id=? AND participant=? AND revoked_at IS NULL', (now, meeting_id, grant['participant']))
    db.execute('INSERT INTO meeting_sessions VALUES(?,?,?,?,NULL)', (room.sha(token), meeting_id, grant['participant'], now + SESSION_TTL))
    room.append(db, row, grant['participant'], 'session_reissued', {'participant_id': participant_id})
    return {'id': meeting_id, 'participant_id': participant_id, 'participant': grant['participant'], 'session_token': token, 'session_expires_at': now + SESSION_TTL}


def issue_seat_invite(db, meeting_id, label='agent', ttl=INVITE_TTL_DEFAULT, *, now=None):
    row = _open(db, meeting_id)
    ttl = _ttl(ttl)
    label = _text(label, 'label', 100)
    now = int(time.time()) if now is None else now
    count = db.execute('SELECT COUNT(*) FROM participants WHERE meeting_id=?', (meeting_id,)).fetchone()[0]
    reserved = db.execute('SELECT COUNT(*) FROM meeting_seat_grants WHERE meeting_id=? AND claimed_at IS NULL AND revoked_at IS NULL AND expires_at>?', (meeting_id, now)).fetchone()[0]
    if count + reserved >= MAX_PARTICIPANTS:
        raise ReviewAPIError('参会席位已满', 409)
    token = secrets.token_urlsafe(32)
    participant_id = uuid.uuid4().hex
    db.execute('INSERT INTO meeting_seat_grants(token_hash,meeting_id,participant_id,label,expires_at) VALUES(?,?,?,?,?)',
               (room.sha(token), meeting_id, participant_id, label, now + ttl))
    room.append(db, row, 'system', 'seat_invite_issued', {'participant_id': participant_id, 'label': label}, created_at=now)
    return {'invite_token': token, 'participant_id': participant_id, 'invite_expires_at': now + ttl, 'invite_mode': 'single-seat'}


def issue_cohost(db, meeting_id, *, now=None):
    row = _open(db, meeting_id)
    now = int(time.time()) if now is None else now
    token, operator = secrets.token_urlsafe(32), 'cohost-' + uuid.uuid4().hex[:12]
    db.execute('INSERT INTO meeting_cohosts VALUES(?,?,?,?,NULL)', (room.sha(token), meeting_id, operator, now + HOST_SESSION_TTL))
    room.append(db, row, 'system', 'cohost_granted', {'operator_id': operator, 'expires_at': now + HOST_SESSION_TTL})
    return {'cohost_token': token, 'operator_id': operator, 'expires_at': now + HOST_SESSION_TTL,
            'grants': ['read', 'comments', 'invites', 'revoke-seat', 'advance', 'waive', 'request-approval']}


def cohost_actor(db, meeting_id, token, action):
    if action not in ('read', 'comments', 'invites', 'revoke-seat', 'advance', 'waive', 'request-approval'):
        return None
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        return None
    row = db.execute('SELECT * FROM meeting_cohosts WHERE token_hash=? AND meeting_id=?', (room.sha(token), meeting_id)).fetchone()
    return row['operator_id'] if row and row['revoked_at'] is None and row['expires_at'] > int(time.time()) else None


def revoke_capability(db, meeting_id, identifier, *, cohost=False):
    row = _open(db, meeting_id)
    if cohost:
        changed = db.execute('UPDATE meeting_cohosts SET revoked_at=? WHERE meeting_id=? AND operator_id=? AND revoked_at IS NULL',
                             (int(time.time()), meeting_id, identifier)).rowcount
    else:
        seat = db.execute('SELECT participant FROM meeting_seat_grants WHERE meeting_id=? AND participant_id=?', (meeting_id, identifier)).fetchone()
        if not seat:
            raise ReviewAPIError('未知席位', 404)
        changed = db.execute('UPDATE meeting_seat_grants SET revoked_at=? WHERE meeting_id=? AND participant_id=? AND revoked_at IS NULL',
                             (int(time.time()), meeting_id, identifier)).rowcount
        db.execute('UPDATE meeting_sessions SET revoked_at=? WHERE meeting_id=? AND participant=? AND revoked_at IS NULL',
                   (int(time.time()), meeting_id, seat['participant']))
    room.append(db, row, 'system', 'cohost_revoked' if cohost else 'seat_revoked', {'identifier': identifier})
    return {'id': meeting_id, 'changed': bool(changed)}


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
           invite_ttl_seconds: int = INVITE_TTL_DEFAULT, now: int | None = None,
           legacy: bool = False) -> dict:
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
    invitation = (issue_invite(db, meeting_id, ttl, now=now) if legacy else
                  issue_seat_invite(db, meeting_id, ttl=ttl, now=now))
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
        # A display name is never a reconnect credential. Retired names stay reserved.
    raise ReviewAPIError("参会席位已满", 409)


def join(db, meeting_id: str, invite_token: str, label: str = "agent",
         *, now: int | None = None, allow_legacy: bool = False, metadata=None, recovery_hash=None) -> dict:
    row = _open(db, meeting_id)
    now = int(time.time()) if now is None else now
    if not isinstance(invite_token, str) or not 32 <= len(invite_token) <= 128:
        raise ReviewAPIError('邀请凭据无效或已过期', 401)
    grant = db.execute('SELECT * FROM meeting_seat_grants WHERE token_hash=? AND meeting_id=?',
                       (room.sha(invite_token), meeting_id)).fetchone()
    if grant:
        if recovery_hash is not None and (not isinstance(recovery_hash, str) or not re.fullmatch('[0-9a-f]{64}', recovery_hash)):
            raise ReviewAPIError('recovery_hash 必须是客户端高熵恢复密钥的 SHA-256')
        if grant['revoked_at'] is not None or grant['expires_at'] <= now:
            raise ReviewAPIError('邀请凭据无效或已过期', 401)
        if grant['claimed_at'] is not None:
            raise ReviewAPIError('邀请已领取；请使用原会话或请主持人重新邀请', 409)
        metadata = metadata or {}
        if not isinstance(metadata, dict) or set(metadata) - {'client', 'model', 'session_label'}:
            raise ReviewAPIError('metadata 仅允许 client/model/session_label')
        if any(not isinstance(v, str) or len(v) > 100 for v in metadata.values()):
            raise ReviewAPIError('metadata 值必须为至多 100 字符的字符串')
        name, _ = _seat_name(db, meeting_id, grant['label'], now)
        count = db.execute('SELECT COUNT(*) FROM participants WHERE meeting_id=?', (meeting_id,)).fetchone()[0]
        if count >= MAX_PARTICIPANTS:
            raise ReviewAPIError('参会席位已满', 409)
        # HTTP caller holds BEGIN IMMEDIATE: consumption and session insertion are atomic.
        updated = db.execute('UPDATE meeting_seat_grants SET claimed_at=?,participant=?,metadata=?,recovery_hash=? WHERE token_hash=? AND claimed_at IS NULL AND revoked_at IS NULL',
                             (now, name, json.dumps(metadata), recovery_hash, room.sha(invite_token))).rowcount
        if updated != 1:
            raise ReviewAPIError('邀请已领取', 409)
        db.execute('INSERT INTO participants VALUES(?,?,?)', (meeting_id, name, row['round']))
        room.append(db, row, 'system', 'invited', {'name': name, 'from_round': row['round'], 'participant_id': grant['participant_id'], 'metadata': metadata, 'identity_source': 'self-reported'})
        room.append(db, row, name, 'joined', {})
        token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO meeting_sessions VALUES(?,?,?,?,NULL)', (room.sha(token), meeting_id, name, now + SESSION_TTL))
        return {'id': meeting_id, 'participant': name, 'participant_id': grant['participant_id'],
                'session_token': token, 'session_expires_at': now + SESSION_TTL,
                'meeting': room.snapshot(db, meeting_id, full=True)}
    if not allow_legacy:
        raise ReviewAPIError('共享邀请已禁用，请向主持人索取独立席位邀请', 401)
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


def check_write_budget(db, meeting_id, actor):
    now = int(time.time())
    recent = db.execute("SELECT COUNT(*) FROM events WHERE meeting_id=? AND actor=? AND kind IN ('comment','review') AND created_at>?", (meeting_id, actor, now - 60)).fetchone()[0]
    if recent >= 10:
        raise ReviewAPIError('发言过于频繁，请稍后重试', 429)
    total = db.execute('SELECT COALESCE(SUM(length(CAST(body AS BLOB))),0) FROM events WHERE meeting_id=?', (meeting_id,)).fetchone()[0]
    if total >= 2_000_000:
        raise ReviewAPIError('会议记录已达到容量上限', 413)


def _write(db, action: str, meeting_id: str, token: str, text: str,
           responds_to_seq: int | None = None, position: str | None = None) -> dict:
    actor = authenticate(db, meeting_id, token)
    _open(db, meeting_id)
    text = _text(text, "text")
    check_write_budget(db, meeting_id, actor)
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
    db.execute('UPDATE meeting_seat_grants SET revoked_at=? WHERE meeting_id=? AND participant=? AND revoked_at IS NULL',
               (int(time.time()) if now is None else now, meeting_id, actor))
    return result

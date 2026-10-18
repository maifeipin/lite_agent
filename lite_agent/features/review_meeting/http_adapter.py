"""
Review Meeting 会审 HTTP Adapter 模块 (features.review_meeting.http_adapter).
将决策评审会审的 HTTP 端点参数校验、权限控制、长轮询挂起与响应逻辑
完整封装在 feature 领域内部，公共宿主仅做传输转发。
"""

import os
import re
import math
import time
import json
import secrets
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

from lite_agent.features.review_meeting import api as access
from lite_agent.features.review_meeting import room

from lite_agent.channels.api import (
    _REVIEW_MAX_WAIT,
    _REVIEW_ONLINE_WINDOW,
    _REVIEW_WAITERS,
    _REVIEW_WAITERS_LOCK,
    _REVIEW_AUTH_ATTEMPTS,
    _REVIEW_AUTH_LOCK,
    _REVIEW_LAST_POLL,
    _REVIEW_POLL_SLOTS,
    _REVIEW_REQUESTS,
)


def review_json(http_handler: Any, value: Any, status: int = 200) -> None:
    body = json.dumps(value, ensure_ascii=False).encode('utf-8')
    try:
        http_handler.send_response(status)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json; charset=utf-8')
        http_handler.send_header('Content-Length', str(len(body)))
        http_handler.send_header('Cache-Control', 'no-store')
        http_handler.send_header('Referrer-Policy', 'no-referrer')
        http_handler.end_headers()
        http_handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def review_body(http_handler: Any) -> dict:
    try:
        size = int(http_handler.headers.get('Content-Length', '0'))
    except ValueError as exc:
        raise ValueError('Content-Length 无效') from exc
    if size <= 0 or size > 65536:
        raise ValueError('JSON 请求体必须为 1 到 65536 字节')
    try:
        value = json.loads(http_handler.rfile.read(size).decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('JSON 请求体无效') from exc
    if not isinstance(value, dict):
        raise ValueError('JSON 请求体必须是对象')
    return value


def review_bearer(http_handler: Any) -> str:
    authorization = http_handler.headers.get('Authorization', '')
    if not authorization.startswith('Bearer ') or authorization.count(' ') != 1:
        return ''
    value = authorization[7:]
    return value if value.isascii() and len(value) <= 128 else ''


def review_admin(http_handler: Any) -> bool:
    api = getattr(getattr(http_handler, "server", None), "api_server", None)
    token = getattr(api, "auth_token", None) if api else None
    bearer = review_bearer(http_handler)
    if token and secrets.compare_digest(bearer, token):
        return True
    if not 32 <= len(bearer) <= 128:
        return False
    db_path = Path(os.environ.get('REVIEW_MEETING_DB', room.DEFAULT_DB))
    try:
        with closing(sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            return access.is_host_session(db, bearer)
    except sqlite3.Error:
        return False


def review_invite_url(http_handler: Any, meeting_id: str, token: str) -> str:
    api = getattr(getattr(http_handler, "server", None), "api_server", None)
    cfg = getattr(api, "config", {}) if api else {}
    port = getattr(api, "port", 8080) if api else 8080
    base = cfg.get('review_meeting_base_url') or f'http://127.0.0.1:{port}'
    parsed = urlparse(base)
    if (parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or (parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost'))):
        raise ValueError('review_meeting_base_url 必须是 HTTPS 地址或本机 HTTP 地址')
    return (base.rstrip('/') + f'/agent/api/v1/review-meetings/{meeting_id}/invite'
            + f'#invite={token}')


def review_operator(http_handler: Any, meeting_id: str, action: str = 'read') -> Any:
    if review_admin(http_handler):
        return 'host'
    if not meeting_id:
        return None
    db_path = Path(os.environ.get('REVIEW_MEETING_DB', room.DEFAULT_DB))
    try:
        with closing(sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            return access.cohost_actor(db, meeting_id, review_bearer(http_handler), action)
    except sqlite3.Error:
        return None


def review_host_allowed(http_handler: Any) -> bool:
    api = getattr(getattr(http_handler, "server", None), "api_server", None)
    cfg = getattr(api, "config", {}) if api else {}
    port = getattr(api, "port", 8080) if api else 8080
    base = cfg.get('review_meeting_base_url') or f'http://127.0.0.1:{port}'
    expected = urlparse(base).hostname
    extra = cfg.get('review_meeting_allowed_hosts') or []
    allowed = {expected, '127.0.0.1', 'localhost'}
    allowed.update(str(h).strip().lower() for h in extra if str(h).strip())
    request_host = (http_handler.headers.get('Host') or '').split(':', 1)[0].lower()
    return request_host in allowed


def review_default_owner_hash(db_path: Path) -> str:
    key_path = db_path.parent / 'owner.key'
    if key_path.exists():
        return room.sha(key_path.read_text(encoding='utf-8').strip())
    return room.new_owner_key(str(key_path))


def review_hint(db, meeting_id: str, snap: dict, seat, is_admin: bool) -> str:
    state = snap.get('state')
    if snap.get('archived_at'):
        return '会议已归档，仅供查阅'
    if state in ('approved', 'rejected'):
        return '会议已裁决' + ('，可 leave 离场' if not is_admin else '')
    if is_admin:
        if state == 'awaiting_approval':
            return '议题待你人工裁决：review-meeting decide --confirm（仅人类可执行）'
        if state == 'changes_requested':
            return '已要求修改：resume 发布新 brief 后讨论继续'
        return '讨论进行中：可在记录弹窗插话引导；全员提交后可 advance 推进轮次'
    if state == 'awaiting_approval':
        return '等待主持人人工裁决，无需动作'
    if state == 'changes_requested':
        return '主持人要求修改，等待新 brief 恢复讨论'
    row = room.get(db, meeting_id)
    done = room.submitted(db, meeting_id, row['round'])
    if seat in done:
        pending = room.missing(db, row)
        tail = f'，等待：{"、".join(pending)}' if pending else '，全员已齐，等待主持人推进轮次'
        return f'本轮已提交正式意见{tail}；可用 comments 自由交流'
    if row['round'] >= 2:
        return '请提交本轮正式意见（reviews）：第 2 轮起必须 responds_to_seq 回应一位评委此前发言'
    return '请阅读 brief 与他人发言，提交本轮正式意见（reviews）'


def review_long_wait(db_path: Path, meeting_id: str, since: int, wait: float, seat):
    if not _REVIEW_POLL_SLOTS.acquire(blocking=False):
        raise access.ReviewAPIError('长轮询并发已满，请稍后重试', 429)
    key = None
    if seat is not None:
        key = (meeting_id, seat)
        with _REVIEW_WAITERS_LOCK:
            if key in _REVIEW_WAITERS:
                _REVIEW_POLL_SLOTS.release()
                raise access.ReviewAPIError('该席位已有挂起的轮询请求', 409)
            _REVIEW_WAITERS[key] = True
    try:
        deadline = time.monotonic() + wait
        uri = f'file:{db_path}?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=30)) as probe:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                row = probe.execute(
                    "SELECT (SELECT COALESCE(MAX(seq),0) FROM events WHERE meeting_id=?),"
                    " (SELECT state FROM meetings WHERE id=?),"
                    " (SELECT archived_at FROM meetings WHERE id=?)",
                    (meeting_id, meeting_id, meeting_id)).fetchone()
                if row[0] > since:
                    return
                if row[1] is None or row[1] in ('approved', 'rejected') or row[2] is not None:
                    return
                time.sleep(min(0.5, remaining))
    finally:
        _REVIEW_POLL_SLOTS.release()
        if key is not None:
            with _REVIEW_WAITERS_LOCK:
                _REVIEW_WAITERS.pop(key, None)


def review_enrich(result: dict, db, meeting_id: str, seat, is_admin: bool):
    now = time.time()
    for p in result.get('participants', []):
        seen = _REVIEW_LAST_POLL.get((meeting_id, p['name']))
        p['last_poll_at'] = int(seen) if seen else None
        p['online'] = bool(seen and now - seen <= _REVIEW_ONLINE_WINDOW)
        grant = db.execute('SELECT participant_id,metadata FROM meeting_seat_grants WHERE meeting_id=? AND participant=?',
                           (meeting_id, p['name'])).fetchone()
        if grant:
            p['participant_id'] = grant['participant_id']
            p['metadata'] = json.loads(grant['metadata'])
            p['identity_source'] = 'self-reported'
    result['hint'] = review_hint(db, meeting_id, result, seat, is_admin)


def handle_review_meeting_api(http_handler: Any, method: str, path: str, query: str) -> None:
    api = getattr(getattr(http_handler, "server", None), "api_server", None)
    cfg = getattr(api, "config", {}) if api else {}

    now = time.monotonic()
    client = http_handler.client_address[0]
    with _REVIEW_AUTH_LOCK:
        for ip in list(_REVIEW_REQUESTS):
            if not _REVIEW_REQUESTS[ip] or now - _REVIEW_REQUESTS[ip][-1] >= 60:
                del _REVIEW_REQUESTS[ip]
        hits = [t for t in _REVIEW_REQUESTS.get(client, []) if now - t < 60]
        limited = len(hits) >= 120 or (client not in _REVIEW_REQUESTS and len(_REVIEW_REQUESTS) >= 2048)
        if not limited:
            hits.append(now)
            _REVIEW_REQUESTS[client] = hits
    if limited:
        review_json(http_handler, {'error': '会议请求过于频繁'}, 429)
        return

    if not review_host_allowed(http_handler):
        review_json(http_handler, {'error': 'Meeting API host is not allowed'}, 421)
        return

    if method == 'POST' and path == '/api/v1/review-meetings/logout':
        try:
            review_body(http_handler)
            db_path = Path(os.environ.get('REVIEW_MEETING_DB', room.DEFAULT_DB))
            with closing(room.connect(db_path)) as db:
                access.ensure_tables(db)
                with db:
                    access.revoke_host_session(db, review_bearer(http_handler))
            review_json(http_handler, {'success': True})
        except access.ReviewAPIError as exc:
            review_json(http_handler, {'error': str(exc)}, exc.status)
        except ValueError as exc:
            review_json(http_handler, {'error': str(exc)}, 400)
        except (OSError, sqlite3.Error):
            review_json(http_handler, {'error': '会审室存储暂不可用'}, 503)
        return

    route = re.fullmatch(
        r'/api/v1/review-meetings(?:/([0-9a-f]{12})(?:/(invite|invites|join|reviews|comments|leave|advance|waive|cohosts|revoke-cohost|revoke-seat|request-approval|recover))?)?',
        path)
    if route is None:
        review_json(http_handler, {'error': 'Not Found'}, 404)
        return
    meeting_id, action = route.groups()
    if method == 'GET' and action == 'invite':
        review_json(http_handler, {
            'meeting_id': meeting_id,
            'join_endpoint': f'/agent/api/v1/review-meetings/{meeting_id}/join',
            'usage': '把链接中的 invite 值作为 POST JSON 的 invite_token；可选 label 为 IDE 名称。'
        })
        return

    try:
        if ((method == 'POST' and action in (None, 'cohosts', 'revoke-cohost'))
                and not review_admin(http_handler)):
            raise access.ReviewAPIError('仅主持人可创建会议或授权共管', 403)
        if ((method == 'POST' and action in ('invites', 'advance', 'waive', 'revoke-seat', 'request-approval'))
                and not review_operator(http_handler, meeting_id, action)):
            raise access.ReviewAPIError('仅主持人可创建会议、更新邀请或推进轮次', 403)
        payload = (review_body(http_handler) if method == 'POST' and action != 'leave'
                   else {})
        db_path = Path(os.environ.get('REVIEW_MEETING_DB', room.DEFAULT_DB))
        with closing(room.connect(db_path)) as db:
            access.ensure_tables(db)
            with db:
                if method == 'POST':
                    db.execute('BEGIN IMMEDIATE')
                    if action in ('invites', 'advance', 'waive', 'revoke-seat', 'request-approval'):
                        actor = review_operator(http_handler, meeting_id, action)
                        if not actor:
                            raise access.ReviewAPIError('共管授权无效或已撤销', 403)
                        room.append(db, room.get(db, meeting_id), actor, 'management_action', {'action': action})
                if method == 'POST' and meeting_id is None:
                    owner_key_hash = payload.get('owner_key_hash')
                    if owner_key_hash is None:
                        owner_key_hash = review_default_owner_hash(db_path)
                    result = access.create(
                        db, title=payload.get('title'), brief=payload.get('brief'),
                        owner_key_hash=owner_key_hash,
                        invite_ttl_seconds=payload.get('invite_ttl_seconds', access.INVITE_TTL_DEFAULT),
                        legacy=cfg.get('review_allow_legacy_invites') is True)
                    result['invite_url'] = review_invite_url(http_handler, result['id'], result.pop('invite_token'))
                    status = 201
                elif method == 'POST' and action == 'invites':
                    result = access.issue_seat_invite(
                        db, meeting_id, payload.get('label', 'agent'), payload.get('invite_ttl_seconds', access.INVITE_TTL_DEFAULT))
                    result['id'] = meeting_id
                    result['invite_url'] = review_invite_url(http_handler, meeting_id, result.pop('invite_token'))
                    status = 201
                elif method == 'POST' and action == 'join':
                    result = access.join(db, meeting_id, payload.get('invite_token'), payload.get('label', 'agent'),
                                         allow_legacy=cfg.get('review_allow_legacy_invites') is True,
                                         metadata=payload.get('metadata'), recovery_hash=payload.get('recovery_hash'))
                    result['protocol'] = (
                        '参会协议：会议期间保持驻留轮询——循环 GET 本会议 ?since=<已读最大seq>&wait=20；'
                        '每次响应后按 hint 字段行动（交 reviews / 发 comments），并立即发起下一次轮询；'
                        '直到 state 为 approved/rejected 或已归档方可退出。'
                        '不要在提交意见后停止轮询，否则无法感知后续讨论。')
                    status = 201
                elif method == 'POST' and action == 'recover':
                    result = access.recover_seat(db, meeting_id, payload.get('participant_id'), payload.get('recovery_secret'))
                    status = 200
                elif method == 'POST' and action == 'cohosts':
                    result = access.issue_cohost(db, meeting_id)
                    status = 201
                elif method == 'POST' and action in ('revoke-cohost', 'revoke-seat'):
                    result = access.revoke_capability(db, meeting_id, payload.get('operator_id' if action == 'revoke-cohost' else 'participant_id'), cohost=action == 'revoke-cohost')
                    status = 200
                elif method == 'POST' and action == 'request-approval':
                    summary = access._text(payload.get('summary'), 'summary')
                    import tempfile
                    with tempfile.TemporaryDirectory(prefix='meeting_approval_') as directory:
                        summary_file = Path(directory) / 'summary.md'
                        summary_file.write_text(summary, encoding='utf-8')
                        result = room.execute(db, SimpleNamespace(action='request-approval', id=meeting_id, summary_file=str(summary_file)))
                    status = 200
                elif method == 'GET' and meeting_id is None:
                    if not review_admin(http_handler):
                        raise access.ReviewAPIError('仅主持人可查看会议列表', 403)
                    rows = db.execute(
                        "SELECT id,title,state,round,created_at,updated_at,archived_at"
                        " FROM meetings ORDER BY updated_at DESC LIMIT 200").fetchall()
                    result = {'meetings': [dict(r) for r in rows]}
                    status = 200
                elif method == 'GET' and meeting_id and action is None:
                    qs = parse_qs(query)
                    value = (qs.get('since') or ['0'])[0]
                    try:
                        since = int(value)
                    except ValueError as exc:
                        raise access.ReviewAPIError('since 必须为非负整数') from exc
                    if since < 0:
                        raise access.ReviewAPIError('since 必须为非负整数')
                    wait_value = (qs.get('wait') or ['0'])[0]
                    try:
                        wait = float(wait_value)
                    except ValueError as exc:
                        raise access.ReviewAPIError('wait 必须为非负秒数') from exc
                    if not math.isfinite(wait) or wait < 0:
                        raise access.ReviewAPIError('wait 必须为非负秒数')
                    wait = min(wait, _REVIEW_MAX_WAIT)
                    is_admin = bool(review_operator(http_handler, meeting_id))
                    seat = None
                    if not is_admin:
                        seat = access.authenticate(db, meeting_id, review_bearer(http_handler))
                        now = time.time()
                        with _REVIEW_WAITERS_LOCK:
                            for key in list(_REVIEW_LAST_POLL):
                                if now - _REVIEW_LAST_POLL[key] > _REVIEW_ONLINE_WINDOW * 2:
                                    del _REVIEW_LAST_POLL[key]
                            _REVIEW_LAST_POLL[(meeting_id, seat)] = now
                    if wait > 0:
                        db.rollback()
                        review_long_wait(db_path, meeting_id, since, wait, seat)
                        db.rollback()
                    if is_admin and not review_operator(http_handler, meeting_id):
                        raise access.ReviewAPIError('主持人会话无效或已过期', 401)
                    if is_admin:
                        result = room.snapshot(db, meeting_id, since=since, full=True)
                    else:
                        result = access.show(db, meeting_id, review_bearer(http_handler), since)
                    review_enrich(result, db, meeting_id, seat, is_admin)
                    status = 200
                elif method == 'POST' and action == 'reviews':
                    result = access.review(
                        db, meeting_id, review_bearer(http_handler), payload.get('text'),
                        payload.get('position'), payload.get('responds_to_seq'))
                    status = 201
                elif method == 'POST' and action == 'comments':
                    operator = review_operator(http_handler, meeting_id, 'comments')
                    if operator:
                        text = access._text(payload.get('text'), 'text')
                        access.check_write_budget(db, meeting_id, 'human' if operator == 'host' else operator)
                        ref = payload.get('responds_to_seq')
                        if ref is not None and (isinstance(ref, bool)
                                                or not isinstance(ref, int) or ref <= 0):
                            raise access.ReviewAPIError('responds_to_seq 必须为正整数', 400)
                        row = room.get(db, meeting_id)
                        if not (operator == 'host' and row['state'] == room.WAITING
                                and row['archived_at'] is None):
                            room.require_open(row)
                        event = room.append(db, row, 'human' if operator == 'host' else operator, 'comment',
                                            {'text': text, 'responds_to_seq': ref})
                        result = {'id': meeting_id, 'event_seq': event['seq'],
                                  'round': event['round'], 'actor': event['actor']}
                    else:
                        result = access.comment(
                            db, meeting_id, review_bearer(http_handler), payload.get('text'),
                            payload.get('responds_to_seq'))
                    status = 201
                elif method == 'POST' and action == 'advance':
                    expected = payload.get('expected_round')
                    row = room.get(db, meeting_id)
                    if isinstance(expected, bool) or not isinstance(expected, int) or expected != row['round']:
                        raise access.ReviewAPIError('expected_round 与当前轮次不一致', 409)
                    result = room.execute(db, SimpleNamespace(action='advance', id=meeting_id))
                    status = 200
                elif method == 'POST' and action == 'waive':
                    names = payload.get('names', '')
                    if isinstance(names, list):
                        names = ','.join(str(n) for n in names)
                    result = room.execute(db, SimpleNamespace(
                        action='waive', id=meeting_id,
                        participants=str(names), reason=str(payload.get('reason') or '')))
                    status = 200
                elif method == 'POST' and action == 'leave':
                    result = access.leave(db, meeting_id, review_bearer(http_handler))
                    status = 200
                else:
                    raise access.ReviewAPIError('Not Found', 404)
        review_json(http_handler, result, status)
    except access.ReviewAPIError as exc:
        review_json(http_handler, {'error': str(exc)}, exc.status)
    except ValueError as exc:
        review_json(http_handler, {'error': str(exc)}, 400)
    except (OSError, sqlite3.Error):
        review_json(http_handler, {'error': '会审室存储暂不可用'}, 503)


def handle_auth(http_handler: Any) -> None:
    now = time.monotonic()
    client = http_handler.client_address[0]
    with _REVIEW_AUTH_LOCK:
        for key in list(_REVIEW_AUTH_ATTEMPTS):
            if not _REVIEW_AUTH_ATTEMPTS[key] or now - _REVIEW_AUTH_ATTEMPTS[key][-1] >= 60:
                del _REVIEW_AUTH_ATTEMPTS[key]
        hits = [t for t in _REVIEW_AUTH_ATTEMPTS.get(client, []) if now - t < 60]
        limited = len(hits) >= 10 or (client not in _REVIEW_AUTH_ATTEMPTS and len(_REVIEW_AUTH_ATTEMPTS) >= 2048)
        if not limited:
            hits.append(now)
            _REVIEW_AUTH_ATTEMPTS[client] = hits
    if limited:
        review_json(http_handler, {'success': False, 'error': '登录请求过多，请稍后重试'}, 429)
        return
    try:
        body = review_body(http_handler)
    except ValueError as exc:
        review_json(http_handler, {'success': False, 'error': str(exc)}, 400)
        return
    username = body.get('username')
    password = body.get('password')
    if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password:
        review_json(http_handler, {'success': False, 'error': '账号和密码不能为空'}, 401)
        return
    username = username.strip()

    srv = getattr(http_handler, "server", None)
    api_srv = getattr(srv, "api_server", None)
    cfg = getattr(api_srv, "config", {}) if api_srv else {}
    if not isinstance(cfg, dict):
        cfg = {}
    htpasswd_path = cfg.get('dashboard_htpasswd_path', '/etc/nginx/conf.d/dashboard.htpasswd')
    if not os.path.exists(htpasswd_path):
        review_json(http_handler, {'success': False, 'error': '服务端配置错误'}, 500)
        return

    try:
        with open(htpasswd_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if ':' not in line:
                    continue
                u, pwd_hash = line.split(':', 1)
                if u != username:
                    continue
                if _verify_htpasswd(password, pwd_hash):
                    db_path = Path(os.environ.get('REVIEW_MEETING_DB', room.DEFAULT_DB))
                    with closing(room.connect(db_path)) as db:
                        access.ensure_tables(db)
                        with db:
                            session = access.issue_host_session(db, username)
                    review_json(http_handler, {'success': True, **session})
                    return
                else:
                    review_json(http_handler, {'success': False, 'error': '账号或密码错误'}, 401)
                    return
    except (OSError, sqlite3.Error):
        review_json(http_handler, {'success': False, 'error': '登录服务暂不可用'}, 503)
        return

    review_json(http_handler, {'success': False, 'error': '账号或密码错误'}, 401)


def _verify_htpasswd(password: str, stored_hash: str) -> bool:
    import hashlib
    if stored_hash.startswith('$2b$') or stored_hash.startswith('$2y$'):
        try:
            import bcrypt
            return bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8'))
        except ImportError:
            return False
    if stored_hash.startswith('$apr1$'):
        parts = stored_hash.split('$')
        if len(parts) < 4:
            return False
        salt = parts[2]
        return stored_hash == _apr1_hash(password, salt)
    if stored_hash.startswith('{SHA}'):
        import base64
        return stored_hash == '{SHA}' + base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
    return password == stored_hash


def _apr1_hash(password: str, salt: str) -> str:
    import hashlib
    ctx = hashlib.md5((password + '$apr1$' + salt).encode('utf-8')).digest()
    ctx = hashlib.md5((password + salt + password).encode('utf-8')).digest()
    for i in range(1000):
        digest = hashlib.md5()
        if i & 1:
            digest.update(password.encode('utf-8'))
        else:
            digest.update(ctx)
        if i % 3:
            digest.update(salt.encode('utf-8'))
        if i % 7:
            digest.update(password.encode('utf-8'))
        if i & 1:
            digest.update(ctx)
        else:
            digest.update(password.encode('utf-8'))
        ctx = digest.digest()
    b64 = './0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
    result = ''
    for a, b, c in ((ctx[i], ctx[i+1] if i+1 < 16 else 0, ctx[i+2] if i+2 < 16 else 0)
                    for i in range(0, 16, 3)):
        result += b64[a & 0x3f]
        result += b64[((a >> 6) & 0x03) | ((b << 2) & 0x3c)]
        result += b64[((b >> 4) & 0x0f)]
        if i + 1 < 16:
            result += b64[((b >> 2) & 0x03) | ((c << 4) & 0x30)]
        if i + 2 < 16:
            result += b64[(c >> 2) & 0x3f]
    return '$apr1$' + salt + '$' + result

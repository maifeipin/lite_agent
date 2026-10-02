"""Deterministic owner commands; never exposed as model tools."""
import json
import os
import secrets
import time
from contextlib import closing
from pathlib import Path

from core.review_meeting_notifications import private_owner_target
from core import review_meeting_api as access
from scripts import review_meeting as room


def handle(config, msg, db_path=None):
    text = msg.text.strip()
    if not text.startswith('/meeting'):
        return None
    parts = text.split(maxsplit=2)
    if parts[0] != '/meeting':
        return None
    target = private_owner_target(msg.channel, config.get('channels', {}))
    if getattr(msg, 'is_guest', True) or not target or str(msg.user_id) != target:
        return '拒绝：会议命令仅供已绑定的本人使用。'
    if msg.channel == 'feishu' and (msg.channel_payload or {}).get('chat_type') != 'p2p':
        return '请在本人私聊中执行会议命令。'
    if msg.channel == 'dingtalk' and (msg.channel_payload or {}).get('msg_data', {}).get('conversationType') != '1':
        return '请在本人私聊中执行会议命令。'
    action = parts[1] if len(parts) > 1 else 'help'
    args = parts[2] if len(parts) > 2 else ''
    if action == 'help':
        return ('/meeting create 标题 | 议题\n/meeting invite 会议ID 席位名称\n'
                '/meeting cohost 会议ID\n/meeting revoke-cohost 会议ID operator_id\n'
                '/meeting revoke-seat 会议ID participant_id\n/meeting status 会议ID\n'
                '/meeting decide 会议ID approve|revise|reject\n/meeting cancel 会议ID\n'
                '/meeting confirm 确认码\n最终批准只记录裁决，不自动执行方案。')
    path = Path(db_path or os.environ.get('REVIEW_MEETING_DB', room.DEFAULT_DB))
    with closing(room.connect(path)) as db:
        access.ensure_tables(db)
        db.execute('''CREATE TABLE IF NOT EXISTS meeting_owner_confirmations (
            token_hash TEXT PRIMARY KEY, channel TEXT, user_id TEXT, meeting_id TEXT,
            decision TEXT, event_seq INTEGER, expires_at INTEGER, used INTEGER DEFAULT 0)''')
        with db:
            db.execute('BEGIN IMMEDIATE')
            if action == 'create':
                title, sep, brief = args.partition('|')
                if not sep:
                    raise ValueError('用法：/meeting create 标题 | 议题')
                key = path.parent / 'owner.key'
                key_hash = room.sha(key.read_text().strip()) if key.exists() else room.new_owner_key(str(key))
                result = access.create(db, title=title.strip(), brief=brief.strip(), owner_key_hash=key_hash)
            elif action in ('invite', 'cohost', 'revoke-cohost', 'revoke-seat'):
                mid, _, label = args.partition(' ')
                if action.startswith('revoke-'):
                    result = access.revoke_capability(db, mid, label.strip(), cohost=action == 'revoke-cohost')
                else:
                    result = (access.issue_seat_invite(db, mid, label.strip()) if action == 'invite'
                              else access.issue_cohost(db, mid))
                result['id'] = mid
            elif action == 'status':
                mid = args.strip()
                snap = room.snapshot(db, mid)
                approval = db.execute("SELECT body FROM events WHERE meeting_id=? AND kind='approval_requested' ORDER BY seq DESC LIMIT 1", (mid,)).fetchone()
                summary = json.loads(approval['body']).get('summary', '') if approval else '尚未申请裁决'
                def compact(value, limit):
                    value = str(value)
                    return value if len(value) <= limit else value[:limit] + '…（已截取）'
                names = '、'.join(p['name'] for p in snap['participants']) or '暂无已领取席位'
                missing = '、'.join(snap['missing']) or '无'
                return (f'会议 {mid}：{compact(snap["title"], 100)}\n状态：{snap["state"]}；第 {snap["round"]} 轮\n'
                        f'参会：{compact(names, 200)}\n未提交：{compact(missing, 200)}\n'
                        f'裁决摘要：{compact(summary, 800)}\n完整记录请通过会议 API 或仪表盘查看。')
            elif action in ('decide', 'cancel'):
                fields = args.split()
                if not fields or (action == 'decide' and len(fields) != 2):
                    raise ValueError('需提供会议ID和裁决 approve/revise/reject')
                mid = fields[0]
                decision = 'cancel' if action == 'cancel' else fields[1]
                if decision not in ('cancel', 'approve', 'revise', 'reject'):
                    raise ValueError('无效裁决')
                row = room.get(db, mid)
                if row['archived_at'] is not None or row['state'] in room.FINAL or (decision != 'cancel' and row['state'] != room.WAITING):
                    raise ValueError('当前会议状态不可裁决')
                seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE meeting_id=?', (mid,)).fetchone()[0]
                code = secrets.token_hex(16)
                db.execute('INSERT INTO meeting_owner_confirmations VALUES(?,?,?,?,?,?,?,0)',
                           (room.sha(code), msg.channel, str(msg.user_id), mid, decision, seq, int(time.time())+300))
                approval = next((e for e in reversed(room.events(db, mid)) if e['kind'] == 'approval_requested'), None)
                summary = approval['body'].get('summary', '') if approval else '取消尚未裁决的讨论'
                return f'会议 {mid}，第 {row["round"]} 轮，裁决 {decision}\n摘要：\n{summary}\n五分钟内由本人发送：/meeting confirm {code}'
            elif action == 'confirm':
                pending = db.execute('SELECT * FROM meeting_owner_confirmations WHERE token_hash=?', (room.sha(args.strip()),)).fetchone()
                if not pending or pending['used'] or pending['expires_at'] <= time.time() or pending['channel'] != msg.channel or pending['user_id'] != str(msg.user_id):
                    raise ValueError('确认码失效或身份/频道不匹配')
                mid = pending['meeting_id']
                row = room.get(db, mid)
                seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE meeting_id=?', (mid,)).fetchone()[0]
                if seq != pending['event_seq'] or row['archived_at'] is not None or row['state'] in room.FINAL or (pending['decision'] != 'cancel' and row['state'] != room.WAITING):
                    raise ValueError('会议已变化，请重新申请裁决确认')
                decision = pending['decision']
                room.append(db, row, 'human', 'cancelled' if decision == 'cancel' else 'decision',
                            {'decision': decision, 'note': '本人频道两步确认', 'channel': msg.channel, 'user_id': str(msg.user_id), 'confirmed_event_seq': seq})
                state = {'approve':'approved', 'revise':'changes_requested', 'reject':'rejected', 'cancel':'rejected'}[decision]
                db.execute('UPDATE meetings SET state=? WHERE id=?', (state, mid))
                if decision == 'cancel':
                    for table in ('meeting_invites','meeting_sessions','meeting_seat_grants','meeting_cohosts'):
                        db.execute(f'UPDATE {table} SET revoked_at=? WHERE meeting_id=? AND revoked_at IS NULL', (int(time.time()), mid))
                db.execute('UPDATE meeting_owner_confirmations SET used=1 WHERE token_hash=?', (pending['token_hash'],))
                return f'已记录本人裁决：{mid} {decision}。未自动执行方案。'
            else:
                raise ValueError('未知命令，发送 /meeting help')
            if 'invite_token' in result:
                base = config.get('channels', {}).get('api', {}).get('review_meeting_base_url', '').rstrip('/')
                from urllib.parse import urlsplit
                if urlsplit(base).scheme != 'https' or not urlsplit(base).netloc:
                    raise ValueError('会议公网地址必须配置为 HTTPS')
                result['invite_url'] = f'{base}/agent/api/v1/review-meetings/{result["id"]}/invite#invite={result.pop("invite_token")}'
            return json.dumps(result, ensure_ascii=False)

"""Durable approval reminders. Only explicitly configured private owner channels.

Unique outbox rows prevent duplicate enqueue, not exactly-once platform delivery.
After an ambiguous platform timeout a bounded retry may deliver a duplicate.
"""
import os
import threading
import time
from contextlib import closing
from pathlib import Path

from scripts import review_meeting as room


def private_owner_target(name, channels_config):
    cfg = channels_config.get(name, {})
    fields = {'feishu': 'admin_open_id', 'wecom': 'admin_userid',
              'wechat': 'admin_wxid', 'dingtalk': 'admin_staff_id',
              'telegram': 'admin_chat_id'}
    uid = str(cfg.get(fields.get(name, ''), '') or '')
    if name == 'telegram':
        uid = str(cfg.get('admin_user_id') or cfg.get('admin_chat_id') or '')
    if name == 'dingtalk':
        uid = uid or str(cfg.get('admin_userid') or '')
    if not uid or any(c in uid for c in ',;\n') or uid == '@all':
        return None
    if name == 'feishu' and not uid.startswith('ou_'):
        return None
    if name == 'telegram' and not uid.isdecimal():
        return None
    return uid if name in fields else None


def deliver_once(agent, config, db_path=None, now=None):
    settings = config.get('review_meeting_notifications', {})
    if settings.get('enabled') is not True:
        return False
    names = settings.get('owner_channels', [])
    if not isinstance(names, list) or not names:
        return False
    targets = []
    for name in names:
        if not isinstance(name, str):
            continue
        uid = private_owner_target(name, config.get('channels', {}))
        channel = next((c for c in agent.channels if c.name == name), None)
        if uid and channel and hasattr(channel, 'send_to'):
            targets.append((channel, uid))
    if not targets:
        return False
    db_path = Path(db_path or os.environ.get('REVIEW_MEETING_DB', room.DEFAULT_DB))
    now = int(time.time()) if now is None else now
    with closing(room.connect(db_path)) as db:
        with db:
            db.execute('BEGIN IMMEDIATE')
            item = db.execute("SELECT * FROM meeting_notification_outbox WHERE state IN ('pending','sending') AND attempts<5 AND next_attempt<=? ORDER BY event_seq LIMIT 1", (now,)).fetchone()
            if not item:
                return False
            meeting = room.get(db, item['meeting_id'])
            latest = db.execute("SELECT MAX(seq) FROM events WHERE meeting_id=? AND kind='approval_requested'", (item['meeting_id'],)).fetchone()[0]
            if meeting['state'] != room.WAITING or meeting['archived_at'] or latest != item['event_seq']:
                db.execute("UPDATE meeting_notification_outbox SET state='cancelled' WHERE meeting_id=? AND event_seq=?", (item['meeting_id'], item['event_seq']))
                return False
            db.execute("UPDATE meeting_notification_outbox SET state='sending',attempts=attempts+1,next_attempt=? WHERE meeting_id=? AND event_seq=?", (now + 180, item['meeting_id'], item['event_seq']))
        # Deliver after commit: CLI and unrelated writers are not blocked on network IO.
        from agent import AgentResponse
        response = AgentResponse(
            f"会议 {item['meeting_id']} 已完成第 {meeting['round']} 轮讨论，等待你裁决。\n"
            f"私聊发送 /meeting status {item['meeting_id']} 查看讨论；本通知不代表批准。",
            title='会议待裁决', color='orange')
        delivered = None
        stale = False
        for channel, uid in targets:
            # Recheck stale approval before each attempt/fallback.
            current = room.get(db, item['meeting_id'])
            latest = db.execute("SELECT MAX(seq) FROM events WHERE meeting_id=? AND kind='approval_requested'", (item['meeting_id'],)).fetchone()[0]
            if current['state'] != room.WAITING or current['archived_at'] or latest != item['event_seq']:
                stale = True
                break
            try:
                if channel.send_to(uid, response):
                    delivered = channel.name
                    break
            except Exception:
                pass
        with db:
            db.execute('UPDATE meeting_notification_outbox SET state=?,next_attempt=?,delivered_channel=? WHERE meeting_id=? AND event_seq=?',
                       ('delivered' if delivered else 'cancelled' if stale else 'failed' if item['attempts'] >= 4 else 'pending', now + min(3600, 60 * 2 ** item['attempts']),
                        delivered, item['meeting_id'], item['event_seq']))
        return bool(delivered)


def start_notifier(agent, config):
    if config.get('review_meeting_notifications', {}).get('enabled') is not True:
        return
    def loop():
        while True:
            try:
                deliver_once(agent, config)
            except Exception as exc:
                print(f'  [Meeting] notification worker error ({type(exc).__name__})')
            time.sleep(5)
    threading.Thread(target=loop, name='MeetingNotifications', daemon=True).start()

from types import SimpleNamespace
from unittest.mock import Mock

from core.review_meeting_notifications import deliver_once, private_owner_target
from scripts import review_meeting as room


def awaiting(db_path, tmp_path):
    brief = tmp_path / 'brief.md'
    brief.write_text('test')
    summary = tmp_path / 'summary.md'
    summary.write_text('summary')
    db = room.connect(db_path)
    with db:
        result = room.execute(db, SimpleNamespace(action='create', title='test', brief_file=str(brief),
                                                  participants='aa,bb', owner_key_hash=room.sha('owner'), owner_key_file=None))
        mid = result['id']
        row = room.get(db, mid)
        first = room.append(db, row, 'aa', 'review', {'position': 'support', 'text': 'a', 'responds_to_seq': None})
        db.execute('UPDATE meetings SET round=2 WHERE id=?', (mid,))
        row = room.get(db, mid)
        room.append(db, row, 'aa', 'review', {'position': 'support', 'text': 'a2', 'responds_to_seq': first['seq']})
        room.append(db, row, 'bb', 'review', {'position': 'support', 'text': 'b2', 'responds_to_seq': first['seq']})
        room.execute(db, SimpleNamespace(action='request-approval', id=mid, summary_file=str(summary)))
    db.close()
    return mid


def test_outbox_produced_from_cli_and_delivered_only_once(tmp_path):
    db_path = tmp_path / 'meeting.db'
    mid = awaiting(db_path, tmp_path)
    channel = SimpleNamespace(name='feishu', send_to=Mock(return_value=True))
    agent = SimpleNamespace(channels=[channel])
    config = {'review_meeting_notifications': {'enabled': True, 'owner_channels': ['feishu']},
              'channels': {'feishu': {'admin_open_id': 'ou_owner'}}}
    assert deliver_once(agent, config, db_path)
    assert not deliver_once(agent, config, db_path)
    assert channel.send_to.call_count == 1
    args = channel.send_to.call_args.args
    assert args[0] == 'ou_owner'
    assert mid in args[1].text


def test_withdrawn_approval_not_sent(tmp_path):
    db_path = tmp_path / 'meeting.db'
    mid = awaiting(db_path, tmp_path)
    db = room.connect(db_path)
    with db:
        db.execute('UPDATE meetings SET state=? WHERE id=?', (room.OPEN, mid))
    db.close()
    channel = SimpleNamespace(name='feishu', send_to=Mock(return_value=True))
    cfg = {'review_meeting_notifications': {'enabled': True, 'owner_channels': ['feishu']},
           'channels': {'feishu': {'admin_open_id': 'ou_owner'}}}
    assert not deliver_once(SimpleNamespace(channels=[channel]), cfg, db_path)
    channel.send_to.assert_not_called()


def test_notification_rejects_group_and_broadcast_targets():
    assert private_owner_target('telegram', {'telegram': {'admin_user_id': '123'}}) == '123'
    assert private_owner_target('telegram', {'telegram': {'admin_user_id': '123', 'admin_chat_id': '-45'}}) == '123'
    assert private_owner_target('feishu', {'feishu': {'admin_open_id': 'oc_group'}}) is None
    assert private_owner_target('telegram', {'telegram': {'admin_chat_id': '-123'}}) is None
    assert private_owner_target('wecom', {'wecom': {'admin_userid': '@all'}}) is None
    assert private_owner_target('wecom', {'wecom': {'admin_userid': 'alice,bob'}}) is None


def test_failed_delivery_falls_back_only_to_configured_owner(tmp_path):
    db_path = tmp_path / 'meeting.db'
    awaiting(db_path, tmp_path)
    failed = SimpleNamespace(name='wechat', send_to=Mock(return_value=False))
    fallback = SimpleNamespace(name='feishu', send_to=Mock(return_value=True))
    outsider = SimpleNamespace(name='wecom', send_to=Mock(return_value=True))
    cfg = {'review_meeting_notifications': {'enabled': True, 'owner_channels': ['wechat', 'feishu']},
           'channels': {'wechat': {'admin_wxid': 'wx_owner'}, 'feishu': {'admin_open_id': 'ou_owner'},
                        'wecom': {'admin_userid': 'other'}}}
    assert deliver_once(SimpleNamespace(channels=[failed, fallback, outsider]), cfg, db_path)
    assert failed.send_to.call_args.args[0] == 'wx_owner'
    assert fallback.send_to.call_args.args[0] == 'ou_owner'
    outsider.send_to.assert_not_called()


def test_notification_failure_retries_are_bounded(tmp_path):
    db_path = tmp_path / 'meeting.db'
    mid = awaiting(db_path, tmp_path)
    channel = SimpleNamespace(name='feishu', send_to=Mock(return_value=False))
    cfg = {'review_meeting_notifications': {'enabled': True, 'owner_channels': ['feishu']},
           'channels': {'feishu': {'admin_open_id': 'ou_owner'}}}
    for i in range(6):
        assert not deliver_once(SimpleNamespace(channels=[channel]), cfg, db_path, now=2_000_000_000 + i * 4000)
    assert channel.send_to.call_count == 5
    db = room.connect(db_path)
    assert db.execute('SELECT state FROM meeting_notification_outbox WHERE meeting_id=?', (mid,)).fetchone()[0] == 'failed'
    db.close()

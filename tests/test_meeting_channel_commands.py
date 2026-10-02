import json
from types import SimpleNamespace
import pytest
from core.meeting_channel_commands import handle
from scripts import review_meeting as room


def test_owner_commands_and_confirmation(tmp_path):
    path = tmp_path / 'room.db'
    cfg = {'channels': {'wechat': {'admin_wxid':'owner'}, 'api': {'review_meeting_base_url':'https://example.com'}}}
    def run(text, guest=False, user='owner'):
        return handle(cfg, SimpleNamespace(text=text, channel='wechat', user_id=user, is_guest=guest, channel_payload={}), path)
    assert '拒绝' in run('/meeting create X | Y', True)
    created = json.loads(run('/meeting create X | Y'))
    mid = created['id']
    invite = json.loads(run(f'/meeting invite {mid} other'))
    assert invite['invite_url'] != created['invite_url']
    cohost = json.loads(run(f'/meeting cohost {mid}'))
    assert 'cohost_token' in cohost
    assert json.loads(run(f'/meeting revoke-cohost {mid} '+cohost['operator_id']))['changed']
    assert json.loads(run(f'/meeting revoke-seat {mid} '+invite['participant_id']))['changed']
    status = run(f'/meeting status {mid}')
    assert '第 1 轮' in status and '完整记录' in status
    assert len(status) < 1600
    prompt = run(f'/meeting cancel {mid}')
    code = prompt.split('/meeting confirm ')[-1]
    assert '拒绝' in run('/meeting confirm '+code, user='other')
    assert '已记录' in run('/meeting confirm '+code)
    with pytest.raises(ValueError):
        run('/meeting confirm '+code)
    with room.connect(path) as db:
        assert room.get(db,mid)['state'] == 'rejected'


def test_stale_confirmation(tmp_path):
    path=tmp_path/'room.db'
    cfg={'channels':{'wechat':{'admin_wxid':'owner'},'api':{'review_meeting_base_url':'https://example.com'}}}
    msg=SimpleNamespace(channel='wechat',user_id='owner',is_guest=False,channel_payload={})
    def run(text):
        msg.text=text
        return handle(cfg,msg,path)
    mid=json.loads(run('/meeting create X | Y'))['id']
    code=run(f'/meeting cancel {mid}').split('/meeting confirm ')[-1]
    run(f'/meeting invite {mid} B')
    with pytest.raises(ValueError,match='已变化'):
        run('/meeting confirm '+code)


def test_approve_requires_waiting_and_records_human(tmp_path):
    path=tmp_path/'room.db'
    cfg={'channels':{'wechat':{'admin_wxid':'owner'},'api':{'review_meeting_base_url':'https://example.com'}}}
    msg=SimpleNamespace(channel='wechat',user_id='owner',is_guest=False,channel_payload={})
    def run(text):
        msg.text=text
        return handle(cfg,msg,path)
    mid=json.loads(run('/meeting create X | Y'))['id']
    with pytest.raises(ValueError):
        run(f'/meeting decide {mid} approve')
    with room.connect(path) as db:
        room.append(db,room.get(db,mid),'system','approval_requested',{'summary':'测试摘要'})
        db.execute('UPDATE meetings SET state=? WHERE id=?',(room.WAITING,mid))
    code=run(f'/meeting decide {mid} approve').split('/meeting confirm ')[-1]
    run('/meeting confirm '+code)
    with room.connect(path) as db:
        assert room.get(db,mid)['state']=='approved'
        assert room.events(db,mid)[-1]['actor']=='human'

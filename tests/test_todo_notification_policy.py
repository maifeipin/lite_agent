import ast
import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from channels.api import ApiHandler
from features.todo.service import TodoService
from features.todo.repository import TodoRepository
from features.billing.repayment_todo import reconcile_repayment_todos
from tests.test_billing_repayment_todo import bill,client_for,rows


def test_muted_bill_sync_keeps_schedule_machine_managed_and_never_duplicates(tmp_path,monkeypatch):
    db=str(tmp_path/'todo.db');client=client_for(bill());reconcile_repayment_todos(client,db)
    service=TodoService(db);tid=rows(db)[0]['id']
    service.update(tid,notify_enabled=False)
    client.run.return_value={'status':'ok','data':{'statements':[bill(updated_at='2026-10-02T00:00:00+00:00',due_date='2026-10-29')]}}
    reconcile_repayment_todos(client,db)
    row=service.get_by_id(tid)
    assert row['notify_enabled']==0 and row['due_at'].startswith('2026-10-26')
    assert not json.loads(row['source_metadata_json']).get('user_edited')
    calls=[];monkeypatch.setattr('features.todo.service.send_im_alert',lambda **kw:calls.append(kw) or True)
    service.tick('2026-10-26T12:00:00+08:00')
    assert not any(c['dedup_key'].startswith('todo_periodic:') for c in calls)
    service.update(tid,notify_enabled=True)
    service.tick('2026-10-26T12:01:00+08:00')
    assert sum(c['dedup_key'].startswith('todo_periodic:') for c in calls)==1
    service.update(tid,notify_enabled=False);service.update(tid,notify_enabled=True)
    reconcile_repayment_todos(client,db);service.tick('2026-10-26T12:02:00+08:00')
    assert sum(c['dedup_key'].startswith('todo_periodic:') for c in calls)==1
    assert service.get_by_id(tid)['status']=='pending'
    client.mark_paid.assert_not_called()


def test_zero_before_is_on_time_and_five_minute_meeting_is_unchanged(tmp_path,monkeypatch):
    service=TodoService(str(tmp_path/'todo.db'));calls=[]
    service.add('meeting',due_at='2026-10-16T10:00:00+08:00',remind_before_mins=40,remind_interval_mins=5)
    service.add('at time',due_at='2026-10-16T10:00:00+08:00',remind_before_mins=0)
    monkeypatch.setattr('features.todo.service.send_im_alert',lambda **kw:calls.append(kw) or True)
    service.tick('2026-10-16T09:19:00+08:00');assert calls==[]
    for minute in range(20,60,5):service.tick(f'2026-10-16T09:{minute:02}:00+08:00')
    assert len(calls)==8 and all('meeting' in c['title'] for c in calls)
    service.tick('2026-10-16T10:00:00+08:00');assert len(calls)==10


def test_mute_is_checked_again_at_atomic_claim(tmp_path):
    service=TodoService(str(tmp_path/'todo.db'))
    service.add('fixture',due_at='2026-10-16T10:00:00+08:00')
    candidate=service.repo.get_due_reminder_candidates()[0]
    service.update(candidate['id'],notify_enabled=False)
    assert not service.repo.mark_notified_due(candidate['id'],'2026-10-16T10:00:00+08:00',None,candidate['due_at'])
    assert service.get_by_id(candidate['id'])['notified_due_at'] is None


def test_muted_notice_can_resume_once_and_summary_still_lists_it(tmp_path,monkeypatch):
    service=TodoService(str(tmp_path/'todo.db'));calls=[]
    first=service.upsert_source('fixture:notice','silent notice',kind='notice',due_at='2026-10-01T00:00:00+00:00',source_metadata={'revision':1})
    service.update(first['id'],notify_enabled=False)
    monkeypatch.setattr('features.todo.service.send_im_alert',lambda **kw:calls.append(kw) or True)
    service.tick('2026-10-16T10:00:00+08:00');assert calls==[]
    assert service.get_by_id(first['id'])['status']=='pending'
    assert 'silent notice' in service.list()
    service.update(first['id'],notify_enabled=True)
    service.tick('2026-10-16T10:01:00+08:00');service.tick('2026-10-16T10:02:00+08:00')
    assert len(calls)==1 and service.get_by_id(first['id'])['status']=='done'


def test_recurring_children_inherit_mute_and_are_still_created(tmp_path,monkeypatch):
    service=TodoService(str(tmp_path/'todo.db'));service.add('template',kind='recurring',notify_enabled=False)
    calls=[];monkeypatch.setattr('features.todo.service.send_im_alert',lambda **kw:calls.append(kw) or True)
    monkeypatch.setattr('features.todo.service._should_spawn_recurring',lambda *a:True)
    result=service.tick('2026-10-16T12:00:00+08:00')
    assert result['spawned']==1 and calls==[]
    with sqlite3.connect(service.db_path) as db:
        child=db.execute('select notify_enabled from todos where recur_parent_id is not null').fetchone()
    assert child==(0,)


@pytest.mark.parametrize('enabled',['false',0,1])
def test_non_boolean_flag_rejected_without_creating_task(tmp_path,enabled):
    service=TodoService(str(tmp_path/'todo.db'))
    with pytest.raises(ValueError):service.add('bad',notify_enabled=enabled)
    assert not Path(service.db_path).exists()


def test_api_rejects_invalid_switch_before_status_mutation(monkeypatch):
    service=Mock();monkeypatch.setattr('features.todo.TodoService',lambda:service)
    body=json.dumps({'status':'done','notify_enabled':'false'}).encode()
    h=SimpleNamespace(headers={'Content-Length':str(len(body))},rfile=io.BytesIO(body),send_error=Mock())
    ApiHandler._handle_patch_todo(h,'/api/v1/todos/fixture')
    h.send_error.assert_called_once_with(400,'notify_enabled must be boolean')
    service.done.assert_not_called()


def test_source_owner_policy_is_generic_and_owner_mismatch_does_not_close(tmp_path):
    service=TodoService(str(tmp_path/'todo.db'))
    first=service.upsert_source('calendar:1','event',created_via='calendar',source_metadata={'revision':1})
    service.update(first['id'],title='human event')
    no=service.resolve_source('calendar:1',revision=2,expected_owner='other',close_user_edited=True)
    assert not no['resolved']
    yes=service.resolve_source('calendar:1',revision=3,expected_owner='calendar',close_user_edited=True)
    assert yes['resolved'] and service.get_by_id(first['id'])['title']=='human event'


def test_platform_boundary_has_no_http_or_billing_rules_in_todo():
    source=Path('features/todo/service.py').read_text();tree=ast.parse(source)
    imports=[node.module or '' for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
    imports += [a.name for node in ast.walk(tree) if isinstance(node,ast.Import) for a in node.names]
    assert not any(name.startswith(('urllib','http.client','core.config_loader')) for name in imports)
    assert '不会自动标记已还款' not in source
    repo=Path('features/todo/repository.py').read_text()
    assert 'billing_repayment:' not in repo and "created_via == 'billing'" not in repo


def test_old_database_rows_keep_notifications_enabled(tmp_path):
    db=str(tmp_path/'legacy.db')
    with sqlite3.connect(db) as connection:
        connection.executescript('''CREATE TABLE todos (
          id TEXT PRIMARY KEY, title TEXT, description TEXT, status TEXT, kind TEXT, project TEXT,
          created_at TEXT, updated_at TEXT, due_at TEXT, notified_due_at TEXT,
          created_via TEXT, recur_parent_id TEXT, recur_cron TEXT, recur_last_spawned_at TEXT, snoozed_until TEXT);
          INSERT INTO todos(id,title,status,kind,created_at,updated_at) VALUES('old','legacy','pending','misc','2026-10-01','2026-10-01');''')
    repo=TodoRepository(db)
    assert repo.get_todo('old')['notify_enabled']==1


def test_generic_daily_reminder_uses_its_own_time_and_timezone(tmp_path,monkeypatch):
    service=TodoService(str(tmp_path/'todo.db'))
    service.add('calendar fixture',kind='reminder',due_at='2026-10-16T15:30:00+00:00')
    calls=[];monkeypatch.setattr('features.todo.service.send_im_alert',lambda **kw:calls.append(kw) or True)
    service.tick('2026-10-16T15:30:00+00:00');assert len(calls)==1
    service.tick('2026-10-17T12:00:00+00:00');assert len(calls)==1
    service.tick('2026-10-17T15:30:00+00:00');assert len(calls)==2
    assert all('还款' not in c['text'] for c in calls)

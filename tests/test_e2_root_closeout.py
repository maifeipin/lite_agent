"""Independent process-budget and competing Todo claim regressions."""
import concurrent.futures
import subprocess
import sys
import threading
import time

import pytest

from integrations.mail.process import OutputLimitExceeded, run_process
from features.todo.repository import TodoRepository


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_process_budget_stops_producer_before_completion(stream):
    script = f"import sys,time\nsys.{stream}.write('x'*200000);sys.{stream}.flush();time.sleep(5)"
    started = time.monotonic()
    with pytest.raises(OutputLimitExceeded):
        run_process([sys.executable, '-c', script], timeout=3, max_output_bytes=1024)
    assert time.monotonic() - started < 3


def test_process_concurrently_drains_pipes_and_times_out():
    result = run_process([sys.executable, '-c', "import sys;print('正文');print('诊断',file=sys.stderr)"])
    assert result.stdout.strip() == '正文'
    assert result.stderr.strip() == '诊断'
    with pytest.raises(subprocess.TimeoutExpired):
        run_process([sys.executable, '-c', 'import time;time.sleep(5)'], timeout=.1)


def test_competing_reminder_and_recurring_claims_have_one_winner(tmp_path):
    repo = TodoRepository(str(tmp_path / 'todos.db'))
    repo.add_todo_atomic('due', 'fixture', due_at='2030-01-01T00:00:00+00:00')
    barrier = threading.Barrier(2)
    def claim(_):
        barrier.wait()
        return repo.mark_notified_due('due','2030-01-01T00:00:00+00:00',None,'2030-01-01T00:00:00+00:00')
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(claim, range(2))) == [False, True]
    repo.add_todo_atomic('parent', 'fixture', kind='recurring', recur_cron='daily')
    barrier = threading.Barrier(2)
    def spawn(n):
        barrier.wait()
        return repo.spawn_recurring_child('child'+str(n),'fixture',None,None,
            '2030-01-01T00:00:00+00:00','parent','2030-01-01T18:00:00+00:00',None)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(spawn, range(2))) == [False, True]
    assert len([r for r in repo.list_todos(status='all') if r['recur_parent_id']=='parent']) == 1


def test_completed_candidate_cannot_claim_reminder(tmp_path):
    repo = TodoRepository(str(tmp_path / 'todos.db'))
    due='2030-01-01T00:00:00+00:00'
    repo.add_todo_atomic('due', 'fixture', due_at=due)
    repo.update_todo_status_atomic('due','done')
    assert not repo.mark_notified_due('due',due,None,due)

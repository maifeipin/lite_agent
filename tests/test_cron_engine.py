from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from core.cron_engine import CronJob, CronManager


def job(expr, enabled=True):
    return CronJob(1, 'fixture', expr, lambda: None, enabled)


def test_fixed_minute_matches_each_hour_and_once_per_minute():
    task = job('7 * * * *')
    assert not task.should_run(datetime(2026, 10, 15, 9, 6))
    assert task.should_run(datetime(2026, 10, 15, 9, 7))
    assert not task.should_run(datetime(2026, 10, 15, 9, 7, 59))
    assert task.should_run(datetime(2026, 10, 15, 10, 7))
    assert task.should_run(datetime(2026, 10, 16, 9, 7))


def test_interval_does_not_confuse_same_minute_in_later_hour_after_pause():
    task = job('*/15 * * * *')
    assert task.should_run(datetime(2026, 10, 15, 9, 0))
    assert task.should_run(datetime(2026, 10, 15, 10, 0))
    assert not task.should_run(datetime(2026, 10, 15, 10, 1))
    assert task.should_run(datetime(2026, 10, 16, 10, 0))


@pytest.mark.parametrize('expr', ['every_minute', '* * * * *'])
def test_every_minute_no_duplicate_ticks(expr):
    task = job(expr)
    assert task.should_run(datetime(2026, 10, 15, 9, 0))
    assert not task.should_run(datetime(2026, 10, 15, 9, 0, 50))
    assert task.should_run(datetime(2026, 10, 15, 9, 1))


@pytest.mark.parametrize('expr', ['12:00', '0 12 * * *', ' 12:00 '])
def test_daily_and_five_field_fixed_hour(expr):
    task = job(expr)
    assert not task.should_run(datetime(2026, 10, 15, 11, 0))
    assert task.should_run(datetime(2026, 10, 15, 12, 0))
    assert not task.should_run(datetime(2026, 10, 15, 12, 0, 30))
    assert task.should_run(datetime(2026, 10, 16, 12, 0))


def test_clock_rollback_does_not_replay_old_slots():
    task = job('7 * * * *')
    assert task.should_run(datetime(2026, 10, 15, 10, 7))
    assert not task.should_run(datetime(2026, 10, 15, 9, 7))
    assert not task.should_run(datetime(2026, 10, 15, 10, 7))
    assert task.should_run(datetime(2026, 10, 15, 11, 7))


def test_timezone_same_instant_is_not_a_second_slot():
    task = job('7 * * * *')
    local = datetime(2026, 10, 15, 12, 7, tzinfo=timezone(timedelta(hours=8)))
    assert task.should_run(local)
    assert not task.should_run(local.astimezone(timezone.utc))
    assert task.should_run(local + timedelta(hours=1))


def test_disabled_job_does_not_consume_slot():
    task = job('every_minute', False)
    now = datetime(2026, 10, 15, 12)
    assert not task.should_run(now)
    task.enabled = True
    assert task.should_run(now)


def test_concurrent_ticks_claim_one_slot():
    task = job('every_minute')
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: task.should_run(datetime(2026, 10, 15, 12)), range(4)))
    assert results.count(True) == 1


@pytest.mark.parametrize('expr', ['', None, 'daily', '25:00', '12:60', '*/0 * * * *', '*/-1 * * * *',
    '*/60 * * * *', '60 * * * *', '0 24 * * *', '7 * * *', '7 * * * 1', '7 * 1 * *', '0 */0 * * *'])
def test_invalid_or_unsupported_expressions_fail_at_registration(expr):
    with pytest.raises(ValueError):
        job(expr)


def test_restart_has_no_persistent_cron_claim_and_no_missed_slot_replay():
    # Persistence belongs to business receipts, not this in-memory scheduler.
    now = datetime(2026, 10, 15, 12, 7)
    assert job('7 * * * *').should_run(now)
    restarted = job('7 * * * *')
    assert restarted.should_run(now)
    assert not restarted.should_run(now + timedelta(minutes=1))


def test_job_exception_keeps_run_lock_clear_and_retries_next_slot():
    manager = object.__new__(CronManager)
    manager._init()
    manager.set_log_callback(lambda text: None)
    callback = Mock(side_effect=[RuntimeError('fixture'), None])
    task = CronJob(1, 'fixture', 'every_minute', callback)
    first = datetime(2026, 10, 15, 12)
    assert task.should_run(first)
    manager._execute_job_task(task)
    assert not task.is_running
    assert not task.should_run(first)
    assert task.should_run(first + timedelta(minutes=1))
    manager._execute_job_task(task)
    assert callback.call_count == 2

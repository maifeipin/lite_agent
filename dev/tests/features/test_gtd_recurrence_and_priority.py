"""Tests for GTD Recurrence Expressions (Cron/Shorthand), Priority levels, and Notification routing."""

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from lite_agent.features.todo.repository import TodoRepository
from lite_agent.features.todo.service import (
    TodoService,
    _parse_cron_field,
    _matches_cron_schedule,
    _parse_target_hm,
    _should_spawn_recurring
)


@pytest.fixture
def temp_repo():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    repo = TodoRepository(path)
    yield repo
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def temp_service(temp_repo):
    return TodoService(temp_repo)


def test_parse_cron_field_syntax():
    # Wildcard
    assert _parse_cron_field("*", 0, 59) == set(range(0, 60))
    # Step on wildcard
    assert _parse_cron_field("*/15", 0, 59) == {0, 15, 30, 45}
    # Specific numbers and ranges
    assert _parse_cron_field("1,5,10-12", 0, 59) == {1, 5, 10, 11, 12}
    # Range with step
    assert _parse_cron_field("10-30/10", 0, 59) == {10, 20, 30}
    # Out of bounds
    assert _parse_cron_field("65", 0, 59) is None
    # Invalid token
    assert _parse_cron_field("abc", 0, 59) is None


def test_matches_cron_schedule_and_aliases():
    # 2026-10-26 is a Monday
    dt_mon = datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)
    # 2026-10-25 is a Sunday
    dt_sun = datetime(2026, 10, 25, 9, 30, tzinfo=timezone.utc)

    # Shorthand daily / every_day
    assert _matches_cron_schedule(dt_mon, "daily") is True
    assert _matches_cron_schedule(dt_sun, "every_day") is True

    # Shorthand workdays / weekdays
    assert _matches_cron_schedule(dt_mon, "workdays") is True
    assert _matches_cron_schedule(dt_sun, "workdays") is False

    # Standard 5-field cron: Mon-Fri at 09:30 -> "30 9 * * 1-5"
    assert _matches_cron_schedule(dt_mon, "30 9 * * 1-5") is True
    assert _matches_cron_schedule(dt_sun, "30 9 * * 1-5") is False

    # Day of month: 26th
    assert _matches_cron_schedule(dt_mon, "30 9 26 * *") is True
    assert _matches_cron_schedule(dt_sun, "30 9 26 * *") is False

    # Step in minutes: */15
    assert _matches_cron_schedule(dt_mon, "*/15 * * * *") is True
    dt_mon_32 = datetime(2026, 10, 26, 9, 32, tzinfo=timezone.utc)
    assert _matches_cron_schedule(dt_mon_32, "*/15 * * * *") is False

    # Monthly shorthand: matches day 1
    dt_first = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    assert _matches_cron_schedule(dt_first, "monthly") is True
    assert _matches_cron_schedule(dt_mon, "monthly") is False


def test_parse_target_hm():
    assert _parse_target_hm("14:30") == "14:30"
    assert _parse_target_hm("30 9 * * 1-5") == "09:30"
    assert _parse_target_hm("daily") == "09:00"
    assert _parse_target_hm("workdays") == "09:00"
    assert _parse_target_hm("invalid") == "09:00"


def test_todo_repo_add_and_update_priority_and_dispatch(temp_repo):
    tid = "test_gtd_001"
    temp_repo.add_todo_atomic(
        tid=tid,
        title="测试GTD高优先级",
        description="重要会议",
        kind="task",
        project="工作",
        due_at="2026-10-26T10:00:00+08:00",
        remind_before_mins=40,
        remind_interval_mins=5,
        notify_enabled=True,
        priority=0,
        dispatch_target="all"
    )

    todo = temp_repo.get_todo(tid)
    assert todo is not None
    assert todo["priority"] == 0
    assert todo["dispatch_target"] == "all"
    assert todo["remind_before_mins"] == 40
    assert todo["remind_interval_mins"] == 5

    # Update fields
    temp_repo.update_todo_fields_atomic(tid, {
        "priority": 2,
        "dispatch_target": "mail"
    })
    updated = temp_repo.get_todo(tid)
    assert updated["priority"] == 2
    assert updated["dispatch_target"] == "mail"


def test_todo_service_dispatch_alert_routing(temp_service):
    with patch("lite_agent.features.todo.service.send_im_alert") as mock_im, \
         patch("lite_agent.integrations.smtp.send_email_delivery") as mock_mail:

        # Case 1: default / im channel
        mock_im.return_value = True
        res = temp_service._dispatch_alert("标题1", "内容1", color="yellow", dispatch_target="im")
        assert res is True
        mock_im.assert_called_once()
        mock_mail.assert_not_called()

        mock_im.reset_mock()
        mock_mail.reset_mock()

        # Case 2: mail channel
        mock_mail.return_value = True
        res = temp_service._dispatch_alert("标题2", "内容2", color="yellow", dispatch_target="mail")
        assert res is True
        mock_mail.assert_called_once_with("标题2", "内容2")
        mock_im.assert_not_called()

        mock_im.reset_mock()
        mock_mail.reset_mock()

        # Case 3: all channels
        mock_im.return_value = True
        mock_mail.return_value = True
        res = temp_service._dispatch_alert("标题3", "内容3", color="red", dispatch_target="all")
        assert res is True
        mock_im.assert_called_once()
        mock_mail.assert_called_once_with("标题3", "内容3")


def test_todo_service_tick_with_priority_and_interval(temp_service):
    # Add a P0 task due at 10:00, remind 40 min before, interval 5 min
    now_dt = datetime(2026, 10, 26, 9, 25, tzinfo=timezone.utc)
    due_dt = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)

    temp_service.add(
        title="紧急项目同步",
        kind="task",
        due_at=due_dt.isoformat(),
        remind_before_mins=40,
        remind_interval_mins=5,
        notify_enabled=True,
        priority=0,
        dispatch_target="channel"
    )

    with patch("lite_agent.features.todo.service.send_im_alert") as mock_im:
        mock_im.return_value = True
        # Tick at 09:25 (which is 35 mins before due, within 40 min window)
        res = temp_service.tick(now_str=now_dt.isoformat())
        assert res["due_notified"] == 1
        assert mock_im.call_count == 1
        call_kwargs = mock_im.call_args[1]
        assert "P0 紧急" in call_kwargs["title"]
        assert call_kwargs["color"] == "red"

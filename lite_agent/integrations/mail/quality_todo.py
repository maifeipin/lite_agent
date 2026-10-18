"""Compatibility re-export for billing quality event consumption.
Canonical implementation relocated to features.billing.quality_todo.
"""

import time
import logging

from lite_agent.features.billing.quality_todo import (
    _format_quality_todo_content,
    consume_quality_events as _impl_consume_quality_events,
    resolve_source_todo,
    upsert_source_todo,
)


def consume_quality_events(client=None, db_path=None, limit=100, stop_on_error=False, deadline=None, **kwargs):
    kwargs.setdefault('upsert_fn', upsert_source_todo)
    kwargs.setdefault('resolve_fn', resolve_source_todo)
    kwargs.setdefault('time_fn', time)
    return _impl_consume_quality_events(client=client, db_path=db_path, limit=limit, stop_on_error=stop_on_error, deadline=deadline, **kwargs)


__all__ = [
    "time",
    "logging",
    "_format_quality_todo_content",
    "consume_quality_events",
    "resolve_source_todo",
    "upsert_source_todo",
]

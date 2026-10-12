"""Compatibility re-export for mailbox sync and quality drain service.
Canonical implementation relocated to features.billing.sync_service per E2 architectural reorganization.
"""

from core import config_loader
from skills import ops_todo
from integrations.mail.client import MailCLI
from features.billing.quality_todo import consume_quality_events
from features.billing.sync_service import (
    _bounded_int,
    drain_quality_events as _impl_drain_quality_events,
    sync_and_consume as _impl_sync_and_consume,
)


def drain_quality_events(client, todo_db_path=None, event_limit=100, max_batches=3, **kwargs):
    if todo_db_path is None:
        cfg = config_loader.load_config() or {}
        todo_db_path = cfg.get('billing', {}).get('todo_db_path') or getattr(ops_todo, 'DB_PATH', None)
    import integrations.mail.quality_todo as qt_mod
    consume_fn = kwargs.pop('consume_fn', None) or consume_quality_events
    kwargs.setdefault('upsert_fn', getattr(qt_mod, 'upsert_source_todo', None))
    kwargs.setdefault('resolve_fn', getattr(qt_mod, 'resolve_source_todo', None))
    return _impl_drain_quality_events(client, todo_db_path=todo_db_path, event_limit=event_limit, max_batches=max_batches, consume_fn=consume_fn, **kwargs)


def sync_and_consume(script_dir=None, statement_db_path=None, todo_db_path=None, months=1, event_limit=100, max_batches=3, client=None, run_fetch=True, runner=None, **kwargs):
    drain_fn = kwargs.pop('drain_fn', None) or drain_quality_events
    return _impl_sync_and_consume(script_dir=script_dir, statement_db_path=statement_db_path, todo_db_path=todo_db_path, months=months, event_limit=event_limit, max_batches=max_batches, client=client, run_fetch=run_fetch, runner=runner, drain_fn=drain_fn, **kwargs)


__all__ = [
    "config_loader",
    "ops_todo",
    "MailCLI",
    "consume_quality_events",
    "_bounded_int",
    "drain_quality_events",
    "sync_and_consume",
]

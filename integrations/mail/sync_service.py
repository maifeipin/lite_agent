"""One mailbox sync followed by bounded, independently retryable quality delivery."""
import os
import time
from typing import Optional, Dict, Any, Callable

from core import config_loader
from core.utils.masker import mask_secrets
from integrations.mail.client import MailCLI
from integrations.mail.quality_todo import consume_quality_events
from skills import ops_todo


def _bounded_int(value, name, ceiling):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'{name} must be a positive integer')
    return min(value, ceiling)


def drain_quality_events(client: MailCLI, todo_db_path: Optional[str] = None,
                         event_limit: int = 100, max_batches: int = 3) -> Dict[str, Any]:
    """Bound work to 300 events; errors/no progress leave receipts for the next run."""
    event_limit = _bounded_int(event_limit, 'event_limit', 100)
    max_batches = _bounded_int(max_batches, 'max_batches', 3)
    stats = dict.fromkeys(('batches', 'total', 'created', 'updated', 'protected',
                          'resolved', 'skipped', 'acked', 'failed', 'failed_acks'), 0)
    stats.update(errors=[], backlog_hit=False, backlog=None,
                 stop_reason='empty', event_limit=event_limit, max_batches=max_batches)
    deadline = time.monotonic() + 30
    for batch_number in range(1, max_batches + 1):
        stats['batches'] = batch_number
        try:
            batch = consume_quality_events(client=client, db_path=todo_db_path, limit=event_limit, stop_on_error=True, deadline=deadline)
        except Exception as exc:
            stats['errors'].append(mask_secrets(f'消费质量事件批次 {batch_number} 异常: {exc}'))
            stats['failed'] += 1
            stats['stop_reason'] = 'error'
            break
        for key in ('total', 'created', 'updated', 'protected', 'resolved',
                    'skipped', 'acked', 'failed', 'failed_acks'):
            stats[key] += batch.get(key, 0)
        stats['errors'].extend(mask_secrets(str(e)) for e in batch.get('errors', []))
        # A failed read can have total=0. It must not masquerade as an empty queue.
        if batch.get('failed', 0) or batch.get('failed_acks', 0) or batch.get('errors'):
            stats['stop_reason'] = 'error'
            break
        if not batch.get('total', 0):
            stats['backlog'] = False
            break
        if not batch.get('acked', 0):
            stats['stop_reason'] = 'no_progress'
            stats['backlog'] = True
            stats['errors'].append('质量事件无 ack 进展，已停止本轮，未处理事件保留待重试')
            break
        # A short batch does not prove the queue is empty: new events can arrive.
        if batch['total'] < event_limit or batch_number == max_batches:
            stats['stop_reason'] = 'limit' if batch_number == max_batches else 'short_batch'
            try:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise TimeoutError('质量待办消费预算耗尽')
                remaining = client.quality_events(limit=1, timeout=budget)
                stats['backlog'] = bool(remaining)
                stats['backlog_hit'] = bool(remaining) and batch_number == max_batches
                if remaining:
                    stats['errors'].append('已达最大消费批次上限，仍有未确认质量事件积压'
                                           if stats['backlog_hit'] else '本轮结束时仍有质量事件待后续消费')
            except Exception as exc:
                stats['failed'] += 1
                stats['errors'].append(mask_secrets(f'检查质量事件积压失败: {exc}'))
                stats['stop_reason'] = 'probe_failed'
            break
    stats['errors'] = list(dict.fromkeys(stats['errors']))
    stats['complete'] = not stats['errors'] and stats['backlog'] is False
    return stats


def sync_and_consume(script_dir: Optional[str] = None,
                     statement_db_path: Optional[str] = None,
                     todo_db_path: Optional[str] = None, months: int = 1,
                     event_limit: int = 100, max_batches: int = 3,
                     client: Optional[MailCLI] = None, run_fetch: bool = True,
                     runner: Optional[Callable] = None) -> Dict[str, Any]:
    """Sync exactly once. Even fetch failure does not prevent existing event delivery."""
    months = _bounded_int(months, 'months', 24)
    event_limit = _bounded_int(event_limit, 'event_limit', 100)
    max_batches = _bounded_int(max_batches, 'max_batches', 3)
    billing = (config_loader.load_config() or {}).get('billing') or {}
    if client is None:
        directory = os.path.abspath(script_dir or billing.get('script_dir') or
                                    '/home/liteagent/mail-statement-parser')
        database = os.path.abspath(statement_db_path or billing.get('statement_db_path') or
                                   os.path.join(directory, 'statements.db'))
        client = MailCLI(script_dir=directory, db_path=database)
    # Injection must not silently substitute a different store for the explicit one.
    elif statement_db_path and isinstance(client, MailCLI):
        if client.db_path is None or os.path.abspath(client.db_path) != os.path.abspath(statement_db_path):
            raise ValueError('Injected MailCLI does not use the requested statement database')
    todo_database = os.path.abspath(todo_db_path or billing.get('todo_db_path') or ops_todo.DB_PATH)
    fetch_log, fetch_errors, summary = '', [], None
    if run_fetch:
        try:
            fetched = client.sync_fetch(months=months, timeout=300, runner=runner)
            summary = fetched.get('summary')
            fetch_errors = [mask_secrets(str(e)) for e in fetched.get('errors', [])]
            if not fetched.get('success', False) and not fetch_errors:
                fetch_errors.append('sync3m 未确认成功，缺少失败详情')
            fetch_log = mask_secrets(f"[sync3m] exit={fetched.get('exit_code')}\n{fetched.get('masked_output') or ''}")
        except Exception as exc:
            fetch_errors = [mask_secrets(f'sync3m: {type(exc).__name__}: {exc}')]
            fetch_log = fetch_errors[0]
    quality = drain_quality_events(client, todo_database, event_limit, max_batches)
    fetch_ok = not run_fetch or not fetch_errors
    quality_ok = quality['complete']
    some_success = bool(quality['acked'] or quality['created'] or
                        (summary and any(a.get('status') == 'ok' for a in summary.get('accounts', []))))
    status = 'ok' if fetch_ok and quality_ok else ('partial' if some_success else 'failed')
    return {'fetch_log': fetch_log, 'fetch_errors': fetch_errors, 'summary': summary,
            'quality_stats': quality, 'quality_errors': quality['errors'],
            'partial': status == 'partial', 'status': status, 'fetch_success': fetch_ok,
            'quality_success': quality_ok, 'backlog_hit': quality['backlog_hit']}

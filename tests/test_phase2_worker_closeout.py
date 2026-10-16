"""Worker ownership, timeout and stale-handle regressions."""

import threading
import pytest


def test_shared_worker_stays_alive_until_last_owner_releases():
    from skills import ops_socks5 as s
    s.stop_failover_worker()
    try:
        first = s.start_failover_worker(poll_interval=60)
        second = s.start_failover_worker(poll_interval=60)
        worker = s._failover_worker_thread
        first()
        assert worker.is_alive(), 'First host shutdown stopped second host worker'
        first()
        assert worker.is_alive(), 'Owner release must be idempotent'
        second()
        worker.join(timeout=1)
        assert not worker.is_alive()
    finally:
        s.stop_failover_worker()


def test_stop_timeout_keeps_ownership_and_blocks_restart():
    from skills import ops_socks5 as s
    s.stop_failover_worker()
    entered, release = threading.Event(), threading.Event()
    worker = None
    def probe():
        entered.set()
        release.wait(timeout=5)
        return {'success': True}
    try:
        s.start_failover_worker(poll_interval=0.001, probe_fn=probe)
        worker = s._failover_worker_thread
        assert entered.wait(timeout=1)
        with pytest.raises(TimeoutError):
            s.stop_failover_worker(timeout=0.01)
        assert s._failover_worker_thread is worker
        assert worker.is_alive()
        with pytest.raises(RuntimeError):
            s.start_failover_worker(poll_interval=60)
    finally:
        release.set()
        if worker:
            worker.join(timeout=1)
        s.stop_failover_worker()


def test_old_owner_handle_cannot_stop_a_new_worker_generation():
    from skills import ops_socks5 as s
    s.stop_failover_worker()
    try:
        old = s.start_failover_worker(poll_interval=60)
        old()
        current = s.start_failover_worker(poll_interval=60)
        worker = s._failover_worker_thread
        old()
        assert worker.is_alive()
        current()
        assert not worker.is_alive()
    finally:
        s.stop_failover_worker()


def test_probe_finishing_after_stop_cannot_switch_or_alert():
    from unittest.mock import Mock, patch
    from skills import ops_socks5 as s
    s.stop_failover_worker()
    entered, release = threading.Event(), threading.Event()
    apply = Mock(return_value=(True, 'ok'))
    alert = Mock()
    worker = None
    def probe():
        entered.set()
        release.wait(timeout=5)
        return {'success': False, 'error': 'review'}
    try:
        with patch.object(s, 'get_current_active_proxy', return_value={'id': 1, 'servername': 'one'}), \
             patch.object(s, 'get_socks5_proxies', return_value=[{'id': 2, 'servername': 'two', 'runcmd': 'naive'}]):
            s._consecutive_failures = 1
            s.start_failover_worker(poll_interval=0.001, probe_fn=probe, apply_fn=apply, alert_fn=alert)
            worker = s._failover_worker_thread
            assert entered.wait(timeout=1)
            with pytest.raises(TimeoutError):
                s.stop_failover_worker(timeout=0.01)
            release.set()
            worker.join(timeout=1)
            assert not worker.is_alive()
            apply.assert_not_called()
            alert.assert_not_called()
    finally:
        release.set()
        if worker:
            worker.join(timeout=1)
        s.stop_failover_worker()

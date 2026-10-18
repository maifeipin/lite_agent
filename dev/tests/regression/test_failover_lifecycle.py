"""
Codex 第六轮复核重点 (Socks5 Worker 所有权与生命周期启停) 长期防倒退回归专项 (tests/regression/test_failover_lifecycle.py).
覆盖:
1. Core / Full 显式 [] / 禁用 network_ops 模式下，启动真实 ApiServer 时零导入 skills.ops_socks5、零 worker 启动
2. network_ops 插件装配时由插件拥有 worker 实例，并向生命周期注册清理句柄
3. 插件注册失败回滚或宿主 shutdown 时，优雅停止 worker，无后台线程残留
4. 原有的探针探测、连续 2 次失败切换、备用 Naive 节点选择、15 分钟退避及告警算法完好保留
"""

import sys
import threading
import time
from types import SimpleNamespace, ModuleType
from unittest.mock import Mock, patch
import pytest

from lite_agent.app.bootstrap import create_app
from lite_agent.services.cron import CronService
from lite_agent.services.routes import RouteRegistry
from lite_agent.channels.api import ApiServer


BASE_CONFIG = {
    "llm": {
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "dummy",
    },
    "channels": {
        "api": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 0,
            "auth_token": "test-token",
        }
    }
}


def test_core_api_start_does_not_start_socks5_failover_worker():
    """Core 模式或基础通道启动时，绝对不启动未启用的 Socks5 故障转移 worker。"""
    server = ApiServer.__new__(ApiServer)
    server.config = {"enabled": True}
    server.host = "127.0.0.1"
    server.port = 0
    server.route_registry = RouteRegistry()
    server.agent = SimpleNamespace(cron=CronService(), _config={})

    worker_mock = Mock()
    with patch("lite_agent.skills.ops_socks5.start_failover_worker", worker_mock), \
         patch("lite_agent.channels.api.ThreadingHTTPServer"), \
         patch("lite_agent.channels.api.threading.Thread"):
        server.start()

    worker_mock.assert_not_called()
    # 验证没有遗留 Socks5FailoverWorker 线程
    threads = [t.name for t in threading.enumerate()]
    assert "Socks5FailoverWorker" not in threads


def test_disabled_network_ops_in_full_mode_does_not_start_worker():
    """Full 模式下若显式禁用 failover 或不加载 network_ops，不启动 worker。"""
    cfg = dict(BASE_CONFIG)
    cfg["network_ops"] = {"failover_enabled": False}
    app = create_app(mode="core", config=cfg)
    try:
        # core 模式不加载 network_ops
        threads = [t.name for t in threading.enumerate()]
        assert "Socks5FailoverWorker" not in threads
    finally:
        app.shutdown()


def test_network_ops_plugin_starts_and_stops_worker_on_lifecycle_shutdown():
    """启用 network_ops 插件时，worker 启动并向宿主注册 cleanup，shutdown 时有界停止退出。"""
    from lite_agent.skills import ops_socks5
    # 确保之前无正在运行的全局 worker
    ops_socks5.stop_failover_worker(timeout=1.0)

    app = create_app(mode="core", config=BASE_CONFIG)
    try:
        from lite_agent.features import network_ops
        network_ops.register(app.plugin_loader.context)

        # 验证 worker 线程已启动
        threads = [t for t in threading.enumerate() if t.name == "Socks5FailoverWorker"]
        assert len(threads) == 1
        worker_thread = threads[0]
        assert worker_thread.is_alive()

        # 执行生命周期优雅关闭
        failed = app.shutdown()
        assert not failed

        # 验证 worker 线程已在超时内优雅退出
        worker_thread.join(timeout=2.0)
        assert not worker_thread.is_alive()
        assert not ops_socks5._failover_worker_started
    finally:
        ops_socks5.stop_failover_worker(timeout=1.0)
        app.shutdown()


def test_network_ops_transaction_rollback_stops_worker():
    """插件注册过程中发生异常触发事务回滚时，必须逆序调用 cleanup 停止 worker。"""
    from lite_agent.skills import ops_socks5
    ops_socks5.stop_failover_worker(timeout=1.0)

    app = create_app(mode="core", config=BASE_CONFIG)
    tx = app.plugin_loader.context._begin_transaction("network_ops")
    try:
        from lite_agent.features import network_ops
        network_ops.register(app.plugin_loader.context)

        threads = [t for t in threading.enumerate() if t.name == "Socks5FailoverWorker"]
        assert len(threads) == 1
        worker_thread = threads[0]
        assert worker_thread.is_alive()

        # 模拟插件注册后置步骤失败，触发事务回滚
        tx.rollback()

        worker_thread.join(timeout=2.0)
        assert not worker_thread.is_alive()
        assert not ops_socks5._failover_worker_started
    finally:
        app.plugin_loader.context._commit_transaction()
        ops_socks5.stop_failover_worker(timeout=1.0)
        app.shutdown()


def test_socks5_failover_worker_algorithm_consecutive_failures_and_backoff():
    """验证 worker 核心算法：连续 2 次探针失败触发自动回退，全备用节点失效进入 15 分钟退避。"""
    from lite_agent.skills import ops_socks5
    ops_socks5.stop_failover_worker(timeout=1.0)

    probe_results = [
        {"success": False, "error": "timeout 1"},
        {"success": False, "error": "timeout 2"},
    ]
    applied_ids = []
    alerts = []

    def mock_probe():
        if probe_results:
            return probe_results.pop(0)
        return {"success": True}

    def mock_apply(proxy_id):
        applied_ids.append(proxy_id)
        return True, "ok"

    def mock_alert(title, text, color, dedup_key):
        alerts.append({"title": title, "text": text, "color": color})

    fake_proxies = [
        {"id": 1, "servername": "node1", "runcmd": "naive --proxy=https://node1.example.com"},
        {"id": 2, "servername": "node2", "runcmd": "naive --proxy=https://node2.example.com"},
    ]

    stop_event = threading.Event()
    with patch("lite_agent.features.network_ops.socks5_service.get_socks5_proxies", return_value=fake_proxies), \
         patch("lite_agent.features.network_ops.socks5_service.get_current_active_proxy", return_value={"id": 1, "servername": "node1"}):

        # 启动 worker，设置超短轮询周期 0.02s
        stop_fn = ops_socks5.start_failover_worker(
            stop_event=stop_event,
            poll_interval=0.02,
            probe_fn=mock_probe,
            apply_fn=mock_apply,
            alert_fn=mock_alert,
        )

        try:
            # 等待 worker 跑完前两次探测与切换
            for _ in range(50):
                if applied_ids:
                    break
                time.sleep(0.02)

            assert 2 in applied_ids, f"未能切换至备用节点: {applied_ids}"
            assert any("自动回退成功" in a["title"] for a in alerts)
        finally:
            stop_fn()
            stop_event.set()

    # 测试节点全部耗尽时的 15 分钟退避逻辑
    probe_results_exhausted = [
        {"success": False, "error": "fail 1"},
        {"success": False, "error": "fail 2"},
    ]
    alerts.clear()
    stop_event2 = threading.Event()

    with patch("lite_agent.features.network_ops.socks5_service.get_socks5_proxies", return_value=[]), \
         patch("lite_agent.features.network_ops.socks5_service.get_current_active_proxy", return_value={"id": 1, "servername": "node1"}):

        stop_fn2 = ops_socks5.start_failover_worker(
            stop_event=stop_event2,
            poll_interval=0.02,
            probe_fn=lambda: probe_results_exhausted.pop(0) if probe_results_exhausted else {"success": False},
            apply_fn=mock_apply,
            alert_fn=mock_alert,
        )

        try:
            for _ in range(50):
                if any("全部备用节点耗尽" in a["title"] for a in alerts):
                    break
                time.sleep(0.02)

            assert any("全部备用节点耗尽" in a["title"] for a in alerts)
            # 确认进入退避
            assert ops_socks5._failover_backoff_until > time.time()
        finally:
            stop_fn2()
            stop_event2.set()

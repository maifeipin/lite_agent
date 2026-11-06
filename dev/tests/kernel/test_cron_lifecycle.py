"""调度器启停与执行交接契约测试 (Stage 3 Commit 5)。

用可控时间 (显式 datetime) 与事件屏障 (threading.Event) 锁定:
- 重复 start 不产生第二个调度线程; stop 幂等且可在未启动时安全调用;
- stop 先置 running=False 停收新任务, 再等待在途任务执行完毕 (wait 语义), 不中断在途;
- 手动触发 (run_job_manually) 与调度循环共用同一执行锁: 在途时拒绝并发执行, 完成后放行;
- 应用 shutdown 只停清自有 cron_service, 不触碰外部注入的独立调度器实例。

已由其他文件锁定、此处不重复的契约:
- 同分钟单次提交 / 并发抢占同一分钟槽位 / 时钟回滚 / 非法表达式:
  kernel/test_cron_service.py
- 跨重启不承诺恰好一次 (内存调度器 at-least-once, 恰好一次由业务回执兜底):
  kernel/test_cron_service.py::test_restart_has_no_persistent_cron_claim_and_no_missed_slot_replay
- 调度循环心跳与在途观测 (health_snapshot): kernel/test_cron_health.py
"""

import threading
import time
from types import ModuleType
from unittest.mock import patch

from lite_agent.app.plugins import PluginLoader
from lite_agent.services.cron import CronService
from lite_agent.app.bootstrap import create_app


def test_stop_before_start_and_double_stop_are_noop():
    svc = CronService()
    svc.stop()  # 未启动: 安全空转
    svc.start()
    svc.stop()
    svc.stop()  # 重复停止: 幂等
    assert svc.running is False
    assert svc.executor is None


def test_double_start_keeps_single_loop_thread():
    svc = CronService()
    svc.start()
    try:
        first_thread = svc._thread
        first_executor = svc.executor
        svc.start()
        assert svc._thread is first_thread, "重复 start 不得新建调度线程"
        assert svc.executor is first_executor, "重复 start 不得重建执行器"
        assert svc.running is True
    finally:
        svc.stop()
    assert not first_thread.is_alive()


def test_stop_blocks_new_work_then_waits_for_inflight():
    """stop 先停收新任务 (running=False 先于在途完成), 再等在途执行完, 不中断在途。"""
    svc = CronService()
    started = threading.Event()
    release = threading.Event()
    done = []

    def blocking():
        started.set()
        release.wait(timeout=10)
        done.append(True)
        return "ok"

    svc.add_job("blocking", "every_minute", blocking)
    svc.start()
    assert started.wait(timeout=5), "调度循环应及时提交到期任务"

    stopper = threading.Thread(target=lambda: svc.stop(wait=True, timeout=5))
    stopper.start()
    deadline = time.time() + 3
    while time.time() < deadline and svc.running:
        time.sleep(0.01)
    assert svc.running is False, "stop 必须先置 running=False 停收新任务"
    assert stopper.is_alive(), "在途任务未完成前, stop(wait=True) 应阻塞等待"
    assert done == []

    release.set()
    stopper.join(timeout=5)
    assert not stopper.is_alive()
    assert done == [True], "在途任务应执行完毕, 不被 stop 中断"
    assert svc.executor is None
    assert not svc._thread.is_alive()


def test_manual_run_shares_execution_lock_with_scheduled_run():
    """手动触发与自动执行共用 job 执行锁: 在途时手动触发被拒绝, 不并发执行。"""
    svc = CronService()
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(timeout=10)
        return "auto-done"

    job_id = svc.add_job("slow", "every_minute", slow)
    auto = threading.Thread(target=svc._execute_job_task, args=(svc.jobs[job_id],))
    auto.start()
    assert started.wait(timeout=5)

    busy_msg = svc.run_job_manually(job_id)
    assert "正在执行" in busy_msg, "在途期间手动触发不得绕过执行锁并发执行"

    release.set()
    auto.join(timeout=5)
    ok_msg = svc.run_job_manually(job_id)
    assert "成功" in ok_msg and "auto-done" in ok_msg


def test_manual_run_serializes_concurrent_manual_triggers():
    """两个并发手动触发同样串行化: 同一时刻只允许一个执行体进入任务函数。"""
    svc = CronService()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        entered.set()
        release.wait(timeout=10)
        return "done"

    job_id = svc.add_job("slow", "every_minute", slow)
    results = []
    first = threading.Thread(target=lambda: results.append(svc.run_job_manually(job_id)))
    first.start()
    assert entered.wait(timeout=5)

    second_msg = svc.run_job_manually(job_id)
    assert "正在执行" in second_msg

    release.set()
    first.join(timeout=5)
    assert calls == [1]
    assert any("成功" in r for r in results)


def test_app_shutdown_stops_and_clears_only_its_own_cron_service():
    """应用 shutdown 只停清自有 cron_service; 外部独立调度器不受影响、仍可独立运行。"""
    external = CronService()
    external.add_job("ext_job", "every_minute", lambda: "ext")

    app = create_app(mode="core", config={})
    app.cron_service.add_job("own_job", "every_minute", lambda: "own")
    app.cron_service.start()

    app.shutdown()

    assert app.cron_service.running is False
    assert app.cron_service.jobs == {}, "应用 shutdown 清空自有任务"
    assert "ext_job" in {j.name for j in external.jobs.values()}, "外部调度器不被应用 clear"

    fired = threading.Event()

    def probe():
        fired.set()
        return "x"

    external.add_job("ext_probe", "every_minute", probe)
    external.start()
    try:
        assert fired.wait(timeout=5), "外部调度器在应用 shutdown 后仍可独立启停运行"
    finally:
        external.stop()


def test_shutdown_drains_inflight_job_before_plugin_resource_cleanup():
    """真实插件资源必须在在途任务结束之后才释放, 而非提前关闭。

    生命周期按注册倒序清理, 插件清理 (后注册) 天然早于宿主资源。若 shutdown
    不在前置阶段停止调度并排空在途任务, 插件资源会先于任务释放。
    """
    order = []
    started = threading.Event()
    release = threading.Event()

    def blocking_job():
        started.set()
        release.wait(timeout=10)
        order.append("job_finished")
        return "ok"

    def plugin_cleanup():
        order.append("plugin_cleanup")

    app = create_app(mode="core", config={})
    app.cron_service.add_job("blocking", "every_minute", blocking_job)
    app.cron_service.start()
    assert started.wait(timeout=5), "调度循环应及时提交到期任务"

    def register(ctx):
        ctx.register_cleanup("plugin_resource", plugin_cleanup)

    plugin = ModuleType("review_plugin")
    plugin.register = register
    loader = PluginLoader(app.plugin_loader.context, {"review_plugin"})
    with patch("lite_agent.app.plugins.importlib.import_module", return_value=plugin):
        loader.load_plugin_by_entry("review_plugin")

    stopper = threading.Thread(target=app.shutdown)
    stopper.start()

    deadline = time.time() + 3
    while time.time() < deadline and app.cron_service.running:
        time.sleep(0.01)
    assert app.cron_service.running is False, "shutdown 必须停止接收新任务"
    assert stopper.is_alive(), "在途任务未完成前, shutdown 应阻塞等待"
    assert order == [], f"在途任务结束前不得释放插件资源, 实际: {order}"

    release.set()
    stopper.join(timeout=5)
    assert not stopper.is_alive()
    assert order == ["job_finished", "plugin_cleanup"], (
        f"必须先排空在途任务再释放插件资源, 实际: {order}"
    )

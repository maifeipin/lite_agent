"""调度器存活观测与构建版本元数据回归测试。

背景: 2026-10-30 05:38 ~ 11-02 20:33 生产进程存活但完全无日志、定时任务全部停摆，
外部监控只检查 systemctl is-active 未能发现。/health 需能暴露调度循环心跳与工作线程占用。
"""

import os
import threading
import time
import unittest
from unittest import mock

from lite_agent.services.cron import CronService
from lite_agent.core import version


class TestCronHealthSnapshot(unittest.TestCase):
    def test_snapshot_before_start(self):
        svc = CronService()
        snap = svc.health_snapshot()
        self.assertFalse(snap["running"])
        self.assertIsNone(snap["loop_age_s"])
        self.assertEqual(snap["busy_workers"], 0)

    def test_loop_heartbeat_and_inflight_tracking(self):
        svc = CronService()
        gate = threading.Event()
        svc.add_job("slow_job", "every_minute", lambda: gate.wait(5))
        svc.start()
        try:
            deadline = time.time() + 3
            snap = svc.health_snapshot()
            while time.time() < deadline and snap["busy_workers"] == 0:
                time.sleep(0.05)
                snap = svc.health_snapshot()
            self.assertTrue(snap["running"])
            self.assertIsNotNone(snap["loop_age_s"])
            self.assertLess(snap["loop_age_s"], 5)
            self.assertEqual(snap["busy_workers"], 1)
            self.assertEqual(snap["longest_job"]["name"], "slow_job")
        finally:
            gate.set()
            svc.stop(wait=True, timeout=2)
        self.assertEqual(svc.health_snapshot()["busy_workers"], 0)

    def test_inflight_cleared_after_exception(self):
        svc = CronService()
        job_id = svc.add_job("boom", "03:00", lambda: 1 / 0)
        svc.executor = None
        svc._execute_job_task(svc.jobs[job_id])
        self.assertEqual(svc.health_snapshot()["busy_workers"], 0)


class TestBuildVersion(unittest.TestCase):
    def setUp(self):
        version._reset_cache_for_tests()

    def tearDown(self):
        version._reset_cache_for_tests()

    def test_env_sha_takes_precedence(self):
        with mock.patch.dict(os.environ, {"LITE_AGENT_BUILD_SHA": "abcdef1234567890"}):
            self.assertEqual(version.get_build_sha(), "abcdef123456")

    def test_fallback_unknown_without_git(self):
        with mock.patch.dict(os.environ, {"LITE_AGENT_BUILD_SHA": ""}), \
                mock.patch.object(version, "_read_git_sha", return_value=None):
            self.assertEqual(version.get_build_sha(), "unknown")

    def test_value_cached_per_process(self):
        with mock.patch.object(version, "_read_git_sha", return_value="11112222") as m, \
                mock.patch.dict(os.environ, {"LITE_AGENT_BUILD_SHA": ""}):
            version.get_build_sha()
            version.get_build_sha()
            self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()


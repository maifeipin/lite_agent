"""
Codex 第五轮复核重点 (V1–V3) 长期防倒退回归专项 (tests/regression/test_legacy_jobs_and_commands.py).
覆盖:
1. V1: 3 类旧 cron 回调在真实配置展开下的分发、去重与无错执行
2. V2: 全部业务 slash 命令（包括 content_ops, sys_ops, mail_ops）向实例 registry 的显式登记与事务注销
3. V3: 默认 full 模式全量生产插件在无环境污染下的完整加载与安全降级
"""

import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

from lite_agent.app.bootstrap import create_app, FULL_MODE_PLUGINS
from lite_agent.services.cron import CronService


BASE_CONFIG = {
    "llm": {
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "dummy",
    }
}


def test_v1_legacy_cron_handlers_registered_and_executable_without_missing_skill_error():
    """V1: config.example.json 声明的旧 cron 任务通过对应 feature 实例专有 handler 分发并可正常执行。"""
    app = create_app(mode="full", config=BASE_CONFIG)
    app.plugin_loader.load_all(FULL_MODE_PLUGINS)
    try:
        # 验证专有 job handler 是否已被 content_ops 和 sys_ops 登记
        assert app.cron_service.get_handler("ops_rss::rss_precompute") is not None
        assert app.cron_service.get_handler("ops_rss::rss_push") is not None
        assert app.cron_service.get_handler("ops_self_check::daily_health") is not None
        assert app.cron_service.get_handler("ops_sentinel::sentinel_scan") is not None

        mock_agent = SimpleNamespace(
            _config={},
            llm=None,
            session_mgr=Mock(),
            channels=[],
            cron_service=app.cron_service,
        )

        h_rss_pre = app.cron_service.get_handler("ops_rss::rss_precompute")
        with patch("lite_agent.features.content_ops.rss_service.rss_precompute", return_value="预计算完成"):
            res = h_rss_pre()
            assert res == "预计算完成"

        h_health = app.cron_service.get_handler("ops_self_check::daily_health")
        with patch("lite_agent.features.sys_ops.health_service.get_health_report", return_value="自检通过"):
            res_h = h_health()
            assert "自检通过" in res_h or "巡检" in res_h
    finally:
        app.shutdown()


def test_v1_cron_registration_deduplication():
    """V1: 重复注册同名或同 slot cron 任务时，必须通过 remove_job_by_name 实现安全幂等去重。"""
    from lite_agent.cli import _register_configured_cron_jobs

    app = create_app(mode="core", config=BASE_CONFIG)
    app.cron_service.register_handler("test_plugin::task1", lambda **kw: "ok")
    mock_agent = SimpleNamespace(cron_service=app.cron_service)

    cfg = {
        "cron_jobs": [
            {"name": "test_job_1", "time": "08:00", "skill": "test_plugin::task1"},
            {"name": "test_job_1", "time": "09:00", "skill": "test_plugin::task1"},
        ]
    }

    try:
        _register_configured_cron_jobs(app, mock_agent, cfg)
        jobs = list(app.cron_service.jobs.values())
        named_jobs = [j for j in jobs if j.name == "test_job_1"]
        assert len(named_jobs) == 1, "同名任务未去重"
        # 验证保留的是最新的调度槽位 09:00
        assert named_jobs[0].cron_expr == "09:00"
    finally:
        app.shutdown()


def test_v2_all_business_slash_commands_registered_in_instance_registry():
    """V2: 全部 20+ 业务 slash 命令（content_ops, sys_ops, mail_ops）均显式登记至实例 command_registry。"""
    app = create_app(mode="full", config=BASE_CONFIG)
    app.plugin_loader.load_all(FULL_MODE_PLUGINS)
    try:
        commands = app.command_registry.commands
        registered_names = set(commands.keys())

        # content_ops (5个)
        expected_content = {"/rss_fetch", "/rss_list", "/rss_log", "/rss_topic", "/sync_meili"}
        for name in expected_content:
            assert name in registered_names, f"命令 {name} 未在实例注册表中注册"

        # sys_ops (2个)
        expected_sys = {"/balance", "/check"}
        for name in expected_sys:
            assert name in registered_names, f"命令 {name} 未在实例注册表中注册"

        # mail_ops (15个)
        expected_mail = {
            "/mail_search", "/mail_reprocess", "/mail_list", "/mail_stats",
            "/mail_fetch", "/mail_fetch_only", "/mail_enrich", "/mail_ok",
            "/mail_noise", "/mail_unnoise", "/mail_noiselist", "/mail_headers",
            "/mail_missed", "/mail_view", "/mail_backfill"
        }
        for name in expected_mail:
            assert name in registered_names, f"邮件命令 {name} 未在实例注册表中注册"

        # 检查是否全部有插件隔离标识
        for name in expected_content | expected_sys | expected_mail:
            cmd = commands[name]
            assert cmd.get("plugin") is not None, f"命令 {name} 未绑定插件标识"
    finally:
        app.shutdown()


def test_v3_full_manifest_loads_safely_without_crashing_on_pymongo():
    """V3: 全量默认 FULL_MODE_PLUGINS 清单可稳定加载，无真实 MongoDB 也能安全降级。"""
    assert len(FULL_MODE_PLUGINS) == 8

    app = create_app(mode="full", config=BASE_CONFIG)
    app.plugin_loader.load_all(FULL_MODE_PLUGINS)
    try:
        loaded = app.loaded_plugins
        for p in FULL_MODE_PLUGINS:
            assert p["entry"] in loaded or p["name"] in loaded, f"插件 {p} 启动加载失败"

        # 验证 ops_meili_sync 在无 pymongo 时的安全降级行为
        from lite_agent.skills.ops_meili_sync import _get_mongodb
        with patch("lite_agent.features.content_ops.meili_sync_service.pymongo", None), \
             patch("lite_agent.skills.ops_meili_sync.pymongo", None):
            with pytest.raises(RuntimeError, match="未安装 pymongo"):
                _get_mongodb()
    finally:
        app.shutdown()

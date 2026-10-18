"""Cold assembly, selector transactions, notification and CLI regressions."""

from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path
import os, subprocess, sys


def test_selector_validates_current_tools_after_plugin_registration(monkeypatch):
    from lite_agent.core.request_selector import RequestSelector
    from lite_agent.core.skill_engine import SkillEngine
    engine = SkillEngine(tools={})
    selector = RequestSelector(engine, domain_map={})
    engine.register_tool('review_tool', {'func': lambda: None, 'schema': {}})
    monkeypatch.setenv('LITE_AGENT_STRICT_SELECTOR', '1')
    selector.register_domain('review', {'pattern': 'review', 'default_tools': ['review_tool']})
    assert selector.select('review').names == ['review_tool']


def test_enrich_tool_uses_host_notification_agent():
    from importlib import reload
    from lite_agent.app.bootstrap import create_app
    from lite_agent.services.commands import CommandRegistry
    from lite_agent.skills import ops_mail_reader as reader
    from lite_agent.features.mail_ops import reader_service
    # The autouse fixture clears decorator metadata between tests while Python
    # retains imported modules. Re-run the real decorators for this fixture.
    with patch('lite_agent.services.commands.default_command_registry', CommandRegistry()):
        reload(reader)
    host = SimpleNamespace(channels=[])
    app = create_app(mode='full', agent=host)
    try:
        app.plugin_loader.load_plugin_by_entry('lite_agent.features.mail_ops', 'mail_ops')
        with patch.object(reader_service, 'run_mail_reader_cmd', return_value='review'), \
             patch.object(reader_service, 'parse_high_importance', return_value='✉️ review'), \
             patch('lite_agent.core.alerts.push_alert', return_value=True) as send:
            app.skill_engine.get_tool('mail_llm_enrich')['func'](limit=3)
        send.assert_called_once()
        assert send.call_args.args[0] is host
    finally:
        app.shutdown()


def test_thin_meeting_cli_starts_outside_repository(tmp_path):
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    script = Path('scripts/review_meeting.py').resolve()
    proc = subprocess.run([sys.executable, '-B', str(script), '--help'], cwd=tmp_path,
                          env=env, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr


from types import SimpleNamespace
from unittest.mock import patch
import pytest


def test_core_app_does_not_inherit_legacy_global_business_domains():
    from lite_agent.app.bootstrap import create_app
    from lite_agent.core.request_selector import DOMAIN_MAP
    spec = {'pattern': 'review', 'default_tools': []}
    with patch.dict(DOMAIN_MAP, {'review_foreign': spec}), patch('lite_agent.agent.MEMORY_AVAILABLE', False):
        app = create_app(mode='core', config={'llm': {'api_key':'dummy', 'base_url':'http://127.0.0.1/v1', 'model':'dummy'}})
        try:
            assert 'review_foreign' not in app.agent.request_selector.domain_map
        finally:
            app.shutdown()


def test_domain_duplicate_is_rejected_without_overwriting_owner():
    from lite_agent.app.bootstrap import create_app
    app = create_app(mode='core')
    ctx = app.plugin_loader.context
    first = {'pattern': 'first', 'default_tools': []}
    try:
        ctx.register_domain('review', first)
        with pytest.raises(ValueError):
            ctx.register_domain('review', {'pattern': 'second', 'default_tools': []})
        assert ctx.domain_registry['review'] == first
    finally:
        app.shutdown()


def test_failed_domain_registration_leaves_no_partial_resource(monkeypatch):
    from lite_agent.app.bootstrap import create_app
    from lite_agent.core.request_selector import RequestSelector
    from lite_agent.core.skill_engine import SkillEngine
    engine = SkillEngine(tools={})
    selector = RequestSelector(engine, domain_map={})
    app = create_app(mode='core', skill_engine=engine, agent=SimpleNamespace(request_selector=selector))
    monkeypatch.setenv('LITE_AGENT_STRICT_SELECTOR', '1')
    try:
        with pytest.raises(RuntimeError):
            app.plugin_loader.context.register_domain('invalid', {'pattern':'invalid', 'default_tools':['absent']})
        assert 'invalid' not in app.domain_registry
        assert 'invalid' not in selector.domain_map
    finally:
        app.shutdown()


PLUGIN_OWNED_TOOLS = {
    'lite_agent.features.mail_ops': [
        'mail_fetch_summaries', 'mail_fetch_only', 'mail_llm_enrich',
        'mail_headers', 'mail_missed', 'mail_view_original', 'backfill_bodies',
        'mail_list', 'mail_search', 'mail_stats', 'mail_reprocess',
    ],
    'lite_agent.features.billing': [
        'billing_report', 'billing_due_soon', 'billing_unpaid', 'billing_mark_paid',
        'billing_reconcile', 'billing_recent', 'billing_snapshot', 'billing_fetch',
        'billing_txns_over', 'billing_parse_health',
    ],
    'lite_agent.features.todo': [
        'todo_add', 'todo_list', 'todo_get', 'todo_start', 'todo_done',
        'todo_drop', 'todo_snooze', 'todo_shelve', 'todo_resume',
        'todo_update', 'todo_dispatch', 'todo_push_brief',
    ],
    'lite_agent.features.network_ops': [
        'socks5_list', 'socks5_add', 'socks5_set_active', 'socks5_get_active',
        'socks5_update', 'socks5_delete', 'socks5_test', 'socks5_test_outbound',
        'edge_cmd', 'edge_query', 'edge_sweep', 'edge_health', 'fleet_audit',
    ],
    'lite_agent.features.sys_ops': [
        'ops_backup_data', 'ops_backup_cloud', 'bypy_info', 'bypy_mkdir',
        'bypy_syncup', 'bypy_syncdown', 'ops_list_crontab', 'ops_run_script',
        'ops_read_logs', 'ops_read_journal', 'ops_sys_status', 'ops_security_audit',
        'ops_self_check', 'sentinel_scan', 'sentinel_baseline', 'sentinel_findings',
        'ops_workspace_run', 'llm_check_balance', 'llm_usage_report',
    ],
    'lite_agent.features.content_ops': [
        'ops_rss_node_status', 'web_clip', 'hedgedoc_upload', 'blog_list_articles',
        'blog_publish_article', 'blog_export_articles', 'sync_meili',
        'ops_media_music_stats', 'ops_media_music_duplicates', 'gaokao_score_rank',
        'gaokao_school_admission', 'gaokao_major_admission', 'gaokao_recommend',
        'gaokao_school_info', 'gaokao_sql', 'gaokao_stats', 'web_search',
        'ops_web_fetch',
    ],
    'lite_agent.features.review_meeting': [
        'ops_review_meeting_join', 'ops_review_meeting_submit',
        'ops_review_meeting_leave', 'ops_review_meeting_show',
        'ops_review_meeting_status', 'ops_review_meeting_comment',
        'ops_decision',
    ],
}


def test_strict_selector_every_single_plugin_loads_cleanly(monkeypatch):
    """验证真实 Agent 在 LITE_AGENT_STRICT_SELECTOR=1 严格模式下，每个插件仅凭自身工具可独立安全加载且零回滚。"""
    from lite_agent.app.bootstrap import create_app
    from lite_agent.app.manifests import FULL_MODE_PLUGINS
    from lite_agent.core.skill_engine import _skill_registry
    monkeypatch.setenv('LITE_AGENT_STRICT_SELECTOR', '1')
    cfg = {'llm': {'api_key': 'dummy', 'base_url': 'http://127.0.0.1/v1', 'model': 'dummy'}}

    for p in FULL_MODE_PLUGINS:
        entry = p['entry']
        # 仅注入该插件自身所拥有的技能，严格测试无跨插件未声明工具依赖
        with patch.dict(_skill_registry, {
            name: {'func': lambda *a, **kw: None, 'schema': {'function': {'name': name}}}
            for name in PLUGIN_OWNED_TOOLS.get(entry, [])
        }, clear=True):
            app = create_app(mode='full', config=cfg)
            try:
                app.plugin_loader.load_plugin_by_entry(entry, p['name'])
                assert entry in app.loaded_plugins
            finally:
                app.shutdown()


def test_strict_selector_full_mode_assembly_cleanly(monkeypatch):
    """验证真实 Agent 在严格模式与非严格模式下，全量插件按清单装配无冲突无遗漏。"""
    from lite_agent.app.bootstrap import create_app
    from lite_agent.app.manifests import FULL_MODE_PLUGINS
    from lite_agent.core.skill_engine import _skill_registry
    cfg = {'llm': {'api_key': 'dummy', 'base_url': 'http://127.0.0.1/v1', 'model': 'dummy'}}

    all_tools = {
        name: {'func': lambda *a, **kw: None, 'schema': {'function': {'name': name}}}
        for tools in PLUGIN_OWNED_TOOLS.values()
        for name in tools
    }

    for strict_val in ('1', '0'):
        monkeypatch.setenv('LITE_AGENT_STRICT_SELECTOR', strict_val)
        with patch.dict(_skill_registry, all_tools, clear=True):
            app = create_app(mode='full', config=cfg)
            try:
                for p in FULL_MODE_PLUGINS:
                    app.plugin_loader.load_plugin_by_entry(p['entry'], p['name'])
                assert len(app.loaded_plugins) == len(FULL_MODE_PLUGINS)
                # 验证所有领域路由均成功载入 selector
                for domain_name in app.domain_registry:
                    assert domain_name in app.agent.request_selector.domain_map
            finally:
                app.shutdown()

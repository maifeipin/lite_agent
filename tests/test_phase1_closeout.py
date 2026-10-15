"""Independent regressions for stage-one boundaries (no production data/network)."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from app.bootstrap import create_app
from app.plugins import PluginContext, PluginLoader
from scripts.check_architecture import (
    ArchitectureVisitor, match_exception, run_checks, verify_exception_requirements,
)


@pytest.mark.parametrize('path,source', [
    ('main.py', 'import features.billing'),
    ('contracts/example.py', 'import requests'),
    ('contracts/example.py', 'from openai import OpenAI'),
    ('contracts/example.py', 'import app'),
    ('contracts/example.py', 'from core.config_loader import load_config'),
    ('contracts/example.py', "import importlib; importlib.import_module('agent')"),
    ('contracts/example.py', "import importlib; importlib.import_module('.bootstrap', package='app')"),
    ('services/example.py', "import importlib; importlib.import_module('agent')"),
    ('core/example.py', "from importlib import import_module as load\nload(name='features.billing')"),
    ('core/example.py', "import importlib as il\nload = il.import_module\nload('features.billing')"),
    ('core/example.py', "import builtins as b\nb.__import__('features.billing')"),
    ('core/example.py', "from builtins import __import__ as load\nload(name='features.billing')"),
])
def test_independent_import_bypasses_rejected(path, source):
    visitor = ArchitectureVisitor(path)
    visitor.visit(ast.parse(source))
    assert visitor.findings, (path, source)


@pytest.mark.parametrize('entries', [None, set(), frozenset()])
def test_empty_plugin_allowlist_denies_before_import(entries):
    loader = PluginLoader(PluginContext(), entries)
    with patch('app.plugins.importlib.import_module') as importer:
        with pytest.raises(PermissionError):
            loader.load_plugin_by_entry('math')
        importer.assert_not_called()


def test_allowed_module_requires_registration_contract():
    loader = PluginLoader(PluginContext(), {'synthetic'})
    with patch('app.plugins.importlib.import_module', return_value=ModuleType('synthetic')):
        with pytest.raises(TypeError):
            loader.load_plugin_by_entry('synthetic')
    assert loader.loaded_plugins == {}


def test_unknown_mode_and_shutdown_reject_loading():
    with pytest.raises(ValueError):
        create_app(mode='typo')
    app = create_app(mode='core')
    app.plugin_loader.allowed_entries = frozenset({'synthetic'})
    app.shutdown()
    with patch('app.plugins.importlib.import_module') as importer:
        with pytest.raises(RuntimeError):
            app.plugin_loader.load_plugin_by_entry('synthetic')
        importer.assert_not_called()


@pytest.mark.parametrize('operation,args', [
    ('register_command', ('/hello', lambda *a: None)),
    ('register_skill', ('echo', {})),
    ('register_cleanup', ('cleanup', lambda: None)),
])
def test_missing_host_registration_service_is_not_silent(operation, args):
    with pytest.raises(RuntimeError):
        getattr(PluginContext(), operation)(*args)


def test_exception_matching_cannot_swallow_submodules_or_rule_aliases():
    exc = dict(file='core/example.py', rule='RULE_NO_CORE_REVERSE_DEPENDENCY',
               target='features.billing', scope='load')
    finding = dict(rule=exc['rule'], target='features.billing.workflow', scope='load')
    assert match_exception(finding, exc['file'], [exc]) is None
    finding.update(target=exc['target'], rule='RULE_NO_KERNEL_BUSINESS_DEPENDENCY')
    assert match_exception(finding, exc['file'], [exc]) is None


def test_frozen_exception_occurrence_limit(tmp_path):
    source = tmp_path / 'core' / 'example.py'
    source.parent.mkdir()
    source.write_text('import features.billing\nimport features.billing\n')
    cfg = tmp_path / 'rules.json'
    cfg.write_text(json.dumps({'exceptions': [dict(
        file='core/example.py', rule='RULE_NO_CORE_REVERSE_DEPENDENCY',
        target='features.billing', scope='<module>', max_occurrences=1,
    )]}))
    _, violations, approved = run_checks(str(tmp_path), str(cfg))
    assert len(violations) == len(approved) == 1
    assert '上限' in violations[0]['detail']


@pytest.mark.parametrize('guard', [
    'if self.allowed_entries and entry_name not in self.allowed_entries:\n            raise PermissionError()',
    "allowed_entries = 'placeholder'",
])
def test_loader_ast_rejects_conditional_or_decorative_guard(guard):
    tree = ast.parse('class PluginLoader:\n'
                     '    def load_plugin_by_entry(self, entry_name):\n'
                     f'        {guard}\n'
                     '        return importlib.import_module(entry_name)\n')
    ok, _ = verify_exception_requirements(
        'app/plugins.py', tree, {'rule': 'RULE_DYNAMIC_IMPORT_REGISTERED'},
        {'scope': 'load_plugin_by_entry'},
    )
    assert not ok


def test_contracts_fresh_process_has_no_runtime_or_write_side_effects(tmp_path):
    """Only contracts source is available; an audit hook rejects I/O side effects."""
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / 'contracts', tmp_path / 'contracts',
                    ignore=shutil.ignore_patterns('__pycache__'))
    script = r'''
import os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
before = set(sys.modules)
def audit(event, args):
    if event.startswith(('socket.', 'sqlite3.')):
        raise AssertionError(event)
    if event == 'open':
        mode, flags = args[1:3]
        if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
            raise AssertionError('write during contracts import')
sys.addaudithook(audit)
import contracts
assert contracts.IncomingMessage
for name in set(sys.modules) - before:
    assert name.split('.')[0] not in {
        'agent', 'core', 'services', 'app', 'features', 'skills', 'channels',
        'integrations', 'openai', 'requests', 'sqlite3', 'socket',
    }, name
print('fresh contracts import passed')
'''
    result = subprocess.run(
        [sys.executable, '-I', '-B', '-c', script, str(tmp_path)],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert 'fresh contracts import passed' in result.stdout


@pytest.mark.parametrize('stream', [False, True])
def test_empty_engine_runs_complete_runtime_conversation(stream):
    from contracts.execution import ExecutionContext
    from core.agent_runtime import AgentRuntime, RuntimeEventType
    from core.model_event import ModelEvent, ModelEventType
    from core.skill_engine import SkillEngine

    with patch.object(SkillEngine, '_load_skills', side_effect=AssertionError('scanned skills')):
        engine = SkillEngine(tools={})
    invoker = MagicMock()
    invoker.invoke_sync.return_value = {
        'content': 'empty-engine reply', 'tool_calls': [], 'finish_reason': 'stop',
        'usage_total': 4, 'empty': False,
    }
    invoker.invoke_stream.return_value = iter([
        ModelEvent(type=ModelEventType.TEXT, data='empty-engine reply'),
        ModelEvent(type=ModelEventType.DONE, data={'finish_reason': 'stop'}),
    ])
    events = list(AgentRuntime(invoker, engine, max_steps=2).run(
        messages=[{'role': 'user', 'content': 'hello'}],
        tools=engine.get_all_schemas(), ctx=ExecutionContext(actor_id='test'), stream=stream,
    ))
    done = [event for event in events if event.type == RuntimeEventType.DONE]
    assert len(done) == 1
    assert done[0].data['content'] == 'empty-engine reply'
    assert not any(event.type == RuntimeEventType.ERROR for event in events)

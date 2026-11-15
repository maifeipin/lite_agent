import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import switch_model as switch


class SwitchModelTests(unittest.TestCase):
    def test_key_update_preserves_profiles_and_rolls_back_on_restart_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path = root / 'llm.json'
                cfg = {'default': 'old', 'api_key': '${OLD_KEY}', 'models': {
                    'old': {'api_key': '${OLD_KEY}'},
                    'flash': {'api_key': '${DEEPSEEK_API_KEY}', 'profiles': {
                        'default': {'invoke_kwargs': {'extra_body': {
                            'thinking': {'type': 'disabled'}}}}}}}}
                path.write_text(json.dumps(cfg))
                original = path.read_bytes()
                with patch.multiple(switch, CONF_D=str(root), LLM_JSON=str(path)), \
                     patch.object(switch, 'restart_service',
                                  side_effect=[RuntimeError('failed'), None] if fail else None), \
                     patch('builtins.print') as output:
                    self.assertEqual(switch.apply_switch('flash', cfg, {}, {}, {}, yes=True), not fail)
                self.assertEqual(cfg['models']['flash']['api_key'], '${DEEPSEEK_API_KEY}')
                if fail:
                    self.assertEqual(path.read_bytes(), original)
                else:
                    saved = json.loads(path.read_text())
                    self.assertNotIn('api_key', saved)
                    # 关键安全断言：切模型坚决不向 JSON 写入明文 Key，保留现有环境变量引用
                    self.assertEqual(saved['models']['flash']['api_key'], '${DEEPSEEK_API_KEY}')
                    self.assertEqual(saved['models']['old'], cfg['models']['old'])
                    self.assertEqual(saved['models']['flash']['profiles'],
                                     cfg['models']['flash']['profiles'])

    def test_cli_rejects_api_key_flag_without_writes_or_restart(self):
        cfg = {'default': 'old', 'models': {'flash': {'aliases': ['ds']}}}
        with patch.object(switch.os.path, 'exists', return_value=True), \
             patch.object(switch, 'load_json', side_effect=[cfg, {}, {}, {}]), \
             patch.object(switch, 'apply_switch') as apply, \
             patch.object(switch, 'restart_service') as restart, \
             patch('builtins.print') as output, \
             patch.object(switch.sys, 'argv', ['switch_model.py', 'DS', '--yes',
                          '--no-restart', '--api-key', 'secret-key-to-reject']), \
             self.assertRaises(SystemExit) as cm:
            switch.main()
        self.assertEqual(cm.exception.code, 1)
        apply.assert_not_called()
        restart.assert_not_called()
        # 绝不把传入的秘密复述在错误信息中
        self.assertNotIn('secret-key-to-reject', str(output.call_args_list))

    def test_apply_switch_rejects_new_api_key(self):
        cfg = {'default': 'old', 'models': {'flash': {}}}
        with patch('builtins.print') as output:
            result = switch.apply_switch('flash', cfg, {}, {}, {}, new_api_key='leak_secret', yes=True)
            self.assertFalse(result)
            self.assertNotIn('leak_secret', str(output.call_args_list))

    def test_success_and_failure_restore_all_files(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                configs = [{'default': 'old', 'models': {'old': {}, 'flash': {}}},
                           {'planner_model': 'old', 'route_rules': [{'model': 'old', 'allowed_models': ['old']}]},
                           {'author_model': 'old', 'validator_model': 'old'}]
                paths = [root / name for name in ('llm.json', 'task_routing.json', 'task_specs.json')]
                for p, data in zip(paths, configs):
                    p.write_text(json.dumps(data))
                    p.chmod(0o600)
                originals = [p.read_bytes() for p in paths]
                with patch.multiple(switch, CONF_D=str(root), LLM_JSON=str(paths[0]),
                                    ROUTING_JSON=str(paths[1]), SPECS_JSON=str(paths[2])), \
                     patch.object(switch, 'restart_service', side_effect=[RuntimeError('failed'), None] if fail else None):
                    self.assertEqual(switch.apply_switch('flash', *configs, {}, yes=True), not fail)
                if fail:
                    self.assertEqual([p.read_bytes() for p in paths], originals)
                else:
                    self.assertEqual(json.loads(paths[0].read_text())['default'], 'flash')
                    self.assertEqual(json.loads(paths[1].read_text())['route_rules'][0]['model'], 'flash')
                self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)

    def test_write_failure_rolls_back_previous_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = [root / n for n in ('llm.json', 'task_routing.json', 'task_specs.json')]
            cfgs = [{'default': 'old', 'models': {'old': {}, 'flash': {}}}, {'planner_model': 'old'}, {}]
            for p, c in zip(paths, cfgs): p.write_text(json.dumps(c))
            original = paths[0].read_bytes()
            real_save = switch.save_json
            def save(path, data):
                if path == str(paths[1]): raise OSError('disk full')
                real_save(path, data)
            with patch.multiple(switch, CONF_D=str(root), LLM_JSON=str(paths[0]), ROUTING_JSON=str(paths[1]), SPECS_JSON=str(paths[2])), patch.object(switch, 'save_json', side_effect=save):
                self.assertFalse(switch.apply_switch('flash', *cfgs, {}, yes=True, restart=False))
            self.assertEqual(paths[0].read_bytes(), original)

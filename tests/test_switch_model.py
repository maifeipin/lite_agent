import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import switch_model as switch


class SwitchModelTests(unittest.TestCase):
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

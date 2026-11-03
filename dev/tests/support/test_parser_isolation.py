"""
单元测试：受控 Parser 隔离守卫行为验证 (dev/tests/support/test_parser_isolation.py)
验证：
1. 固定 Parser Git SHA 真实校验；
2. 异常失败时可靠恢复进入前状态（原对象保留，新增对象 pop）；
3. tmp_path 边界约束与环境变量恢复。
"""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dev.tests.support.parser_isolation import (
    isolated_parser_context,
    verify_parser_fixed_version,
    MANAGED_PARSER_MODULES,
    PINNED_PARSER_SHA,
)


class TestParserIsolation(unittest.TestCase):
    def test_01_verify_parser_fixed_version_success(self):
        """正例：当前仓库匹配固定 PINNED_PARSER_SHA"""
        candidate = (
            os.environ.get("MAIL_STATEMENT_PARSER_DIR")
            or "/Users/lilee/Projects/dev/mail-statement-parser"
        )
        root = Path(candidate)
        if (root / ".git").exists():
            sha = verify_parser_fixed_version(root, expected_sha=PINNED_PARSER_SHA)
            self.assertTrue(sha.startswith(PINNED_PARSER_SHA))

    def test_02_verify_parser_fixed_version_mismatch_fails(self):
        """反例：SHA 不匹配时必须抛出 AssertionError 严格拦截"""
        candidate = (
            os.environ.get("MAIL_STATEMENT_PARSER_DIR")
            or "/Users/lilee/Projects/dev/mail-statement-parser"
        )
        root = Path(candidate)
        if (root / ".git").exists():
            with self.assertRaises(AssertionError) as ctx:
                verify_parser_fixed_version(root, expected_sha="deadbeef00000000000000000000000000000000")
            self.assertIn("未锁定在已核准基线", str(ctx.exception))

    def test_03_state_restoration_preserves_preexisting_module_objects(self):
        """正例：验证若进入前已有同名模块，退出时恢复原对象；非原有模块则 pop"""
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            
            # 模拟进入前 sys.modules 已存在一个假对象
            sentinel_obj = object()
            fake_mod_name = "statement_db"
            orig_obj = sys.modules.get(fake_mod_name)
            sys.modules[fake_mod_name] = sentinel_obj

            orig_path = list(sys.path)

            try:
                os.environ["TESTING"] = "1"
                with isolated_parser_context(tmp_path) as harness:
                    # 进入后，应该被替换为真实加载的模块
                    self.assertIsNot(sys.modules[fake_mod_name], sentinel_obj)
                    self.assertIsNotNone(harness.sd)

                # 退出后，sys.modules[fake_mod_name] 必须恢复为 sentinel_obj，而不是被 pop
                self.assertIs(sys.modules.get(fake_mod_name), sentinel_obj)
                self.assertEqual(sys.path, orig_path)
            finally:
                if orig_obj is None:
                    sys.modules.pop(fake_mod_name, None)
                else:
                    sys.modules[fake_mod_name] = orig_obj

    def test_04_cleanup_on_initialization_failure(self):
        """反例：初始化过程抛出异常时，现场必须无条件清理恢复"""
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            orig_path = list(sys.path)

            sentinel_mod = "statement_evidence"
            self.assertNotIn(sentinel_mod, sys.modules)

            os.environ["TESTING"] = "1"
            with patch("dev.tests.support.parser_isolation.verify_parser_fixed_version", side_effect=RuntimeError("SHA check error")):
                with self.assertRaises(RuntimeError):
                    with isolated_parser_context(tmp_path):
                        pass

            # 异常发生后，sys.path 和 sys.modules 均已恢复
            self.assertEqual(sys.path, orig_path)
            self.assertNotIn(sentinel_mod, sys.modules)

    def test_05_cleanup_on_mid_import_failure(self):
        """反例：模块导入中途抛出异常时，已导入的模块必须清理，原对象与环境必须恢复"""
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            orig_path = list(sys.path)
            sentinel_obj = object()
            fake_mod_name = "statement_db"
            orig_obj = sys.modules.get(fake_mod_name)
            sys.modules[fake_mod_name] = sentinel_obj

            try:
                os.environ["TESTING"] = "1"
                with patch(
                    "importlib.import_module",
                    side_effect=ImportError("Simulated failure loading parser module"),
                ):
                    with self.assertRaises(ImportError):
                        with isolated_parser_context(tmp_path):
                            pass

                # 验证退出后：sentinel_obj 恢复，sys.path 恢复，env 恢复
                self.assertIs(sys.modules.get(fake_mod_name), sentinel_obj)
                self.assertEqual(sys.path, orig_path)
                self.assertNotIn("MAIL_STATEMENT_DATA_ROOT", os.environ)
            finally:
                if orig_obj is None:
                    sys.modules.pop(fake_mod_name, None)
                else:
                    sys.modules[fake_mod_name] = orig_obj


if __name__ == "__main__":
    unittest.main()

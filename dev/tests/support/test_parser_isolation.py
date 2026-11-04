"""
单元测试：受控 Parser 隔离守卫行为验证 (dev/tests/support/test_parser_isolation.py)
验证：
1. 固定 Parser Git SHA 真实校验；
2. 异常失败时可靠恢复进入前状态（原对象保留，新增对象 pop）；
3. tmp_path 严格临时目录边界校验（防范持久目录与符号链接逃逸）；
4. 与全局 pytest 环境隔离，精确核对进入前后的环境变量快照。
"""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dev.tests.support.parser_isolation import (
    isolated_parser_context,
    is_valid_temporary_test_dir,
    verify_parser_fixed_version,
    MANAGED_PARSER_MODULES,
    MANAGED_PARSER_ENV_KEYS,
    PINNED_PARSER_SHA,
)


class TestParserIsolation(unittest.TestCase):
    def setUp(self):
        # 记录测试进入前的全局环境快照，防止与全局 conftest 冲突或污染后续用例
        self._orig_env = {k: os.environ.get(k) for k in MANAGED_PARSER_ENV_KEYS}
        self._orig_testing = os.environ.get("TESTING")

    def tearDown(self):
        # 严格复原测试进入前的环境变量与 TESTING 标志
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

        if self._orig_testing is None:
            os.environ.pop("TESTING", None)
        else:
            os.environ["TESTING"] = self._orig_testing

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
                verify_parser_fixed_version(
                    root, expected_sha="deadbeef00000000000000000000000000000000"
                )
            self.assertIn("未锁定在已核准基线", str(ctx.exception))

    def test_02b_verify_parser_fixed_version_snapshot_metadata(self):
        """正反例：脱敏只读快照无 .git 时通过固化元数据 .git_commit_sha 验证 SHA"""
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            (snap_dir / "mail_client.py").write_text("# entry", encoding="utf-8")
            (snap_dir / ".git_commit_sha").write_text(f"{PINNED_PARSER_SHA}\n", encoding="utf-8")

            # 正例：快照中的固化 SHA 与 PINNED_PARSER_SHA 匹配
            sha = verify_parser_fixed_version(snap_dir, expected_sha=PINNED_PARSER_SHA)
            self.assertEqual(sha, PINNED_PARSER_SHA)

            # 反例：快照中的固化 SHA 不匹配
            with self.assertRaises(AssertionError) as ctx:
                verify_parser_fixed_version(
                    snap_dir, expected_sha="deadbeef00000000000000000000000000000000"
                )
            self.assertIn("未锁定在已核准基线", str(ctx.exception))

            # 反例：无 .git 且无 .git_commit_sha 时抛出 RuntimeError 拒绝无法核验的仓库
            (snap_dir / ".git_commit_sha").unlink()
            with self.assertRaises(RuntimeError) as ctx:
                verify_parser_fixed_version(snap_dir, expected_sha=PINNED_PARSER_SHA)
            self.assertIn("无法获取 Parser 仓库 Git SHA", str(ctx.exception))

    def test_03_state_restoration_preserves_preexisting_module_objects(self):
        """正例：验证若进入前已有同名模块，退出时恢复原对象；非原有模块则 pop；环境还原进入前快照"""
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            env_before = {k: os.environ.get(k) for k in MANAGED_PARSER_ENV_KEYS}

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
                    # 验证上下文内部已重定向至临时目录
                    self.assertEqual(
                        os.environ.get("MAIL_STATEMENT_DATA_ROOT"), str(tmp_path)
                    )

                # 退出后，sys.modules[fake_mod_name] 必须恢复为 sentinel_obj，而不是被 pop
                self.assertIs(sys.modules.get(fake_mod_name), sentinel_obj)
                self.assertEqual(sys.path, orig_path)
                # 环境变量必须精准恢复为进入前的快照（无论 conftest 是否预设）
                env_after = {k: os.environ.get(k) for k in MANAGED_PARSER_ENV_KEYS}
                self.assertEqual(env_after, env_before)
            finally:
                if orig_obj is None:
                    sys.modules.pop(fake_mod_name, None)
                else:
                    sys.modules[fake_mod_name] = orig_obj

    def test_04_cleanup_on_initialization_failure(self):
        """反例：初始化过程抛出异常时，现场必须无条件清理恢复为进入前快照"""
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            orig_path = list(sys.path)
            env_before = {k: os.environ.get(k) for k in MANAGED_PARSER_ENV_KEYS}

            sentinel_mod = "statement_evidence"
            self.assertNotIn(sentinel_mod, sys.modules)

            os.environ["TESTING"] = "1"
            with patch(
                "dev.tests.support.parser_isolation.verify_parser_fixed_version",
                side_effect=RuntimeError("SHA check error"),
            ):
                with self.assertRaises(RuntimeError):
                    with isolated_parser_context(tmp_path):
                        pass

            # 异常发生后，sys.path、sys.modules 和环境变量均已恢复为进入前快照
            self.assertEqual(sys.path, orig_path)
            self.assertNotIn(sentinel_mod, sys.modules)
            env_after = {k: os.environ.get(k) for k in MANAGED_PARSER_ENV_KEYS}
            self.assertEqual(env_after, env_before)

    def test_05_cleanup_on_mid_import_failure(self):
        """反例：模块导入中途抛出异常时，已导入的模块必须清理，原对象与环境必须恢复为进入前快照"""
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            orig_path = list(sys.path)
            env_before = {k: os.environ.get(k) for k in MANAGED_PARSER_ENV_KEYS}

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

                # 验证退出后：sentinel_obj 恢复，sys.path 恢复，环境恢复为进入前快照
                self.assertIs(sys.modules.get(fake_mod_name), sentinel_obj)
                self.assertEqual(sys.path, orig_path)
                env_after = {k: os.environ.get(k) for k in MANAGED_PARSER_ENV_KEYS}
                self.assertEqual(env_after, env_before)
            finally:
                if orig_obj is None:
                    sys.modules.pop(fake_mod_name, None)
                else:
                    sys.modules[fake_mod_name] = orig_obj

    def test_06_is_valid_temporary_test_dir_accepts_valid_temp_dirs(self):
        """正例：验证真实系统临时目录与活跃测试根被正确识别允许"""
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(is_valid_temporary_test_dir(td))
            self.assertTrue(is_valid_temporary_test_dir(Path(td)))
            sub_dir = Path(td) / "nested" / "deep_dir"
            self.assertTrue(is_valid_temporary_test_dir(sub_dir))

        sys_temp = Path(tempfile.gettempdir())
        self.assertTrue(is_valid_temporary_test_dir(sys_temp))

    def test_07_is_valid_temporary_test_dir_rejects_persistent_and_symlink_escape(self):
        """反例：非受控持久目录、伪装前缀目录及符号链接逃逸必须被严格拦截"""
        # 1. 持久系统目录必须拒绝
        self.assertFalse(is_valid_temporary_test_dir("/var/lib"))
        self.assertFalse(is_valid_temporary_test_dir("/var/lib/mysql"))
        self.assertFalse(is_valid_temporary_test_dir("/var/www"))
        self.assertFalse(is_valid_temporary_test_dir("/etc"))
        self.assertFalse(is_valid_temporary_test_dir(PROJECT_ROOT))

        # 2. 伪装 /tmp-evil 前缀目录必须拒绝
        self.assertFalse(is_valid_temporary_test_dir("/tmp-evil"))
        self.assertFalse(is_valid_temporary_test_dir("/var/tmp-evil/bad"))

        # 3. 符号链接逃逸拦截验证
        with tempfile.TemporaryDirectory() as td:
            escape_link = os.path.join(td, "escape_to_var_lib")
            try:
                os.symlink("/var/lib", escape_link)
                # 真实路径解析后指向 /var/lib，绝不能放行
                self.assertFalse(is_valid_temporary_test_dir(escape_link))
                self.assertFalse(is_valid_temporary_test_dir(Path(escape_link) / "escaped_sub"))
            except (OSError, NotImplementedError):
                pass

        # 4. 验证 isolated_parser_context 在持久路径上直接抛出 AssertionError
        os.environ["TESTING"] = "1"
        with self.assertRaises(AssertionError) as ctx:
            with isolated_parser_context(Path("/var/lib/dangerous_target")):
                pass
        self.assertIn("禁止非临时或符号链接逃逸路径", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

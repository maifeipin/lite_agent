"""
邮件与账单外部解释器路由解析与 SQLite/Chroma 环境保护测试 (test_mail_parser_executable.py)。
使用纯 Python 标准库 unittest.TestCase，无任何第三方测试框架依赖。
"""

import os
import sys
import tempfile
import stat
import unittest
from unittest import mock

from lite_agent.integrations.mail.paths import get_parser_python_executable
from lite_agent.features.billing.service import run_billing_cmd
from lite_agent.core.sqlite_utils import safe_chromadb_sqlite_env, _MIN_CHROMADB_SQLITE_VERSION


class TestMailParserExecutableAndSqliteGuard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_executable_file(self, filename: str, executable: bool = True) -> str:
        filepath = os.path.join(self.base, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho ok\n")
        mode = 0o755 if executable else 0o644
        os.chmod(filepath, mode)
        return filepath

    def test_explicit_env_valid_executable_returns_absolute_path(self):
        """显式环境变量设置有效可执行文件时返回绝对路径"""
        fake_py = self._create_executable_file("my_python", executable=True)
        with mock.patch.dict(os.environ, {"MAIL_STATEMENT_PARSER_PYTHON": fake_py}):
            chosen = get_parser_python_executable()
            self.assertEqual(chosen, os.path.abspath(fake_py))

    def test_explicit_env_nonexistent_raises_file_not_found(self):
        """显式环境变量指定不存在路径时必须抛出 FileNotFoundError，绝不静默降级"""
        nonexistent = os.path.join(self.base, "not_exist_py")
        with mock.patch.dict(os.environ, {"MAIL_STATEMENT_PARSER_PYTHON": nonexistent}):
            with self.assertRaises(FileNotFoundError):
                get_parser_python_executable()

    def test_explicit_env_not_executable_raises_permission_error(self):
        """显式环境变量指定文件缺少执行权限时必须抛出 PermissionError，绝不静默降级"""
        fake_py = self._create_executable_file("no_exec_py", executable=False)
        with mock.patch.dict(os.environ, {"MAIL_STATEMENT_PARSER_PYTHON": fake_py}):
            with self.assertRaises(PermissionError):
                get_parser_python_executable()

    def test_explicit_config_nonexistent_raises_file_not_found(self):
        """配置文件 billing.python_executable 指定不存在路径时必须抛出异常"""
        nonexistent = os.path.join(self.base, "missing_cfg_py")
        fake_cfg = {"billing": {"python_executable": nonexistent}}
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("lite_agent.core.config_loader.load_config", return_value=fake_cfg):
            with self.assertRaises(FileNotFoundError):
                get_parser_python_executable()

    def test_unconfigured_auto_discovers_venv_with_executable_permission(self):
        """未配置时自动发现 script_dir/.venv/bin/python"""
        script_dir = os.path.join(self.base, "parser_proj")
        venv_bin = os.path.join(script_dir, ".venv", "bin")
        os.makedirs(venv_bin, exist_ok=True)
        venv_py = os.path.join(venv_bin, "python")
        with open(venv_py, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(venv_py, 0o755)

        with mock.patch.dict(os.environ, {"MAIL_STATEMENT_PARSER_PYTHON": ""}):
            with mock.patch("lite_agent.core.config_loader.load_config", return_value={}):
                chosen = get_parser_python_executable(script_dir=script_dir)
                self.assertEqual(chosen, os.path.abspath(venv_py))

    def test_unconfigured_fallback_to_system_or_sys_executable(self):
        """未配置且无独立 venv 时，受控回退至可用系统解释器或当前解释器"""
        with mock.patch.dict(os.environ, {"MAIL_STATEMENT_PARSER_PYTHON": ""}):
            with mock.patch("lite_agent.core.config_loader.load_config", return_value={}):
                chosen = get_parser_python_executable(script_dir=self.base)
                self.assertTrue(os.path.isfile(chosen))
                self.assertTrue(os.access(chosen, os.X_OK))

    def test_billing_service_run_cmd_invokes_designated_parser_python(self):
        """验证 billing.service.run_billing_cmd 使用 get_parser_python_executable 解析出的解释器"""
        fake_py = self._create_executable_file("custom_billing_py", executable=True)
        fake_script = os.path.join(self.base, "mail_client.py")
        with open(fake_script, "w", encoding="utf-8") as f:
            f.write("print('billing mock')\n")

        with mock.patch.dict(os.environ, {"MAIL_STATEMENT_PARSER_PYTHON": fake_py}), \
             mock.patch("lite_agent.features.billing.service.get_billing_env", return_value={
                 "mail_client_py": fake_script,
                 "script_dir": self.base,
             }), \
             mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="OK", stderr="")
            res = run_billing_cmd(["report", "3"])
            self.assertEqual(res, "OK")
            # 必须使用 fake_py 而非硬编码 python3
            called_args = mock_run.call_args[0][0]
            self.assertEqual(called_args[0], fake_py)
            self.assertEqual(called_args[1], fake_script)
            self.assertEqual(called_args[2:], ["report", "3"])

    def test_safe_chromadb_sqlite_env_skips_monkeypatch_when_sqlite_is_modern(self):
        """当内置标准库 sqlite_version_info >= (3, 35, 0) 时，必须直接跳过 pysqlite3 注入"""
        import sqlite3 as _stdlib_sqlite3
        orig_modules_sqlite3 = sys.modules.get("sqlite3")
        with safe_chromadb_sqlite_env() as yielded:
            # 现代版本直接返回标准库 sqlite3，且 sys.modules 未被篡改
            if _stdlib_sqlite3.sqlite_version_info >= _MIN_CHROMADB_SQLITE_VERSION:
                self.assertIs(yielded, _stdlib_sqlite3)
                self.assertIs(sys.modules.get("sqlite3"), orig_modules_sqlite3)


if __name__ == "__main__":
    unittest.main()

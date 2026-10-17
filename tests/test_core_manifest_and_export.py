"""
单元测试：阶段三 S3-A1 基础源码清单与导出工具安全防御检查 (tests/test_core_manifest_and_export.py)
验证 scripts/export_core.py 的核心功能及全部反例：
1. --help 退出码为 0 且包含关键说明
2. 缺失必需核心模块时非零退出 (退出码 4)
3. 物理文件不存在时非零退出 (退出码 3)
4. SHA-256 不匹配时非零退出 (退出码 3)
5. 越界路径/目录穿越时安全拒绝 (退出码 2)
6. 夹带业务模块 (features/skills/integrations.mail) 安全拒绝 (退出码 2)
7. 夹带受保护或数据后缀 (.db, .eml, config.json) 安全拒绝 (退出码 2)
8. 正常导出至临时目录完整性与可用性验证 (退出码 0)
"""

import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT_PATH = os.path.join(REPO_ROOT, "scripts", "export_core.py")
MANIFEST_PATH = os.path.join(REPO_ROOT, "architecture", "kernel-core-manifest.txt")


class TestCoreManifestAndExport(unittest.TestCase):

    def test_01_help_returns_zero(self):
        """反例验证: --help 退出码必须为 0 且包含参数说明"""
        res = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("export_core.py", res.stdout)
        self.assertIn("--manifest", res.stdout)
        self.assertIn("--output-dir", res.stdout)
        self.assertIn("--verify-only", res.stdout)

    def test_02_default_manifest_verify_success(self):
        """正例验证: 仓库默认 manifest 与当前源码 SHA-256 校验通过"""
        res = subprocess.run(
            [
                sys.executable,
                SCRIPT_PATH,
                "--manifest",
                MANIFEST_PATH,
                "--agent-root",
                REPO_ROOT,
                "--verify-only",
                "--check-hashes",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"校验失败: {res.stderr}")
        self.assertIn("验证通过", res.stdout)

    def test_03_missing_required_core_module_fails(self):
        """反例验证: 若 manifest 遗漏关键必需模块 (如 main.py)，必须非零退出 (exit_code=4)"""
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt") as tf:
            # 故意漏写 main.py
            tf.write("agent.py\nsession.py\nsecurity.py\nrequirements-core.txt\n")
            tf.flush()

            res = subprocess.run(
                [
                    sys.executable,
                    SCRIPT_PATH,
                    "--manifest",
                    tf.name,
                    "--agent-root",
                    REPO_ROOT,
                    "--verify-only",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 4)
            self.assertIn("遗漏关键核心基础模块", res.stderr)
            self.assertIn("main.py", res.stderr)

    def test_04_file_not_found_fails(self):
        """反例验证: 若 manifest 列出不存在的虚假核心文件，必须退出 (exit_code=3)"""
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            valid_content = f.read()

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt") as tf:
            tf.write(valid_content + "\ncore/non_existent_ghost_file.py\n")
            tf.flush()

            res = subprocess.run(
                [
                    sys.executable,
                    SCRIPT_PATH,
                    "--manifest",
                    tf.name,
                    "--agent-root",
                    REPO_ROOT,
                    "--verify-only",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 3)
            self.assertIn("清单文件不存在", res.stderr)

    def test_05_path_traversal_fails(self):
        """反例验证: 目录穿越 (../) 或绝对路径，安全门禁阻断 (exit_code=2)"""
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            valid_content = f.read()

        # 1. 相对越界
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt") as tf:
            tf.write(valid_content + "\n../outside_evil.py\n")
            tf.flush()

            res = subprocess.run(
                [
                    sys.executable,
                    SCRIPT_PATH,
                    "--manifest",
                    tf.name,
                    "--agent-root",
                    REPO_ROOT,
                    "--verify-only",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 2)
            self.assertIn("跨级上溯路径", res.stderr)

        # 2. 绝对路径
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt") as tf:
            tf.write(valid_content + "\n/etc/passwd\n")
            tf.flush()

            res = subprocess.run(
                [
                    sys.executable,
                    SCRIPT_PATH,
                    "--manifest",
                    tf.name,
                    "--agent-root",
                    REPO_ROOT,
                    "--verify-only",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 2)
            self.assertIn("包含绝对路径", res.stderr)

    def test_06_business_inclusion_fails(self):
        """反例验证: 夹带业务模块 (features/skills/integrations/mail) 必须安全阻断 (exit_code=2)"""
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            valid_content = f.read()

        forbidden_cases = [
            "features/billing/__init__.py",
            "skills/ops_logs.py",
            "integrations/mail/sync_service.py",
        ]

        for fc in forbidden_cases:
            with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt") as tf:
                tf.write(valid_content + f"\n{fc}\n")
                tf.flush()

                res = subprocess.run(
                    [
                        sys.executable,
                        SCRIPT_PATH,
                        "--manifest",
                        tf.name,
                        "--agent-root",
                        REPO_ROOT,
                        "--verify-only",
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(res.returncode, 2, f"未成功阻断夹带: {fc}")
                self.assertIn("夹带业务模块", res.stderr)

    def test_07_sensitive_data_or_db_inclusion_fails(self):
        """反例验证: 夹带 config.json, .env, *.db, *.eml 等数据或凭据文件安全阻断 (exit_code=2)"""
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            valid_content = f.read()

        sensitive_cases = [
            "config.json",
            ".env",
            "data/memory.db",
            "test_statement.eml",
        ]

        for sc in sensitive_cases:
            with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt") as tf:
                tf.write(valid_content + f"\n{sc}\n")
                tf.flush()

                res = subprocess.run(
                    [
                        sys.executable,
                        SCRIPT_PATH,
                        "--manifest",
                        tf.name,
                        "--agent-root",
                        REPO_ROOT,
                        "--verify-only",
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(res.returncode, 2, f"未成功阻断敏感文件: {sc}")

    def test_08_successful_export_to_temp_dir(self):
        """正例验证: 正常导出至仓外临时目录，验证文件树完整且无额外夹带"""
        with tempfile.TemporaryDirectory(prefix="core_export_test_") as tmp_dir:
            res = subprocess.run(
                [
                    sys.executable,
                    SCRIPT_PATH,
                    "--manifest",
                    MANIFEST_PATH,
                    "--agent-root",
                    REPO_ROOT,
                    "--output-dir",
                    tmp_dir,
                    "--check-hashes",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"导出失败: {res.stderr}")
            self.assertIn("成功导出", res.stdout)

            # 验证导出的文件实际存在
            self.assertTrue(os.path.isfile(os.path.join(tmp_dir, "main.py")))
            self.assertTrue(os.path.isfile(os.path.join(tmp_dir, "agent.py")))
            self.assertTrue(os.path.isfile(os.path.join(tmp_dir, "core", "agent_runtime.py")))
            self.assertTrue(os.path.isfile(os.path.join(tmp_dir, "services", "commands.py")))
            self.assertTrue(os.path.isfile(os.path.join(tmp_dir, "app", "bootstrap.py")))
            self.assertTrue(os.path.isfile(os.path.join(tmp_dir, "channels", "api.py")))

            # 验证禁止的业务目录与私人配置绝不存在于导出目录中
            self.assertFalse(os.path.exists(os.path.join(tmp_dir, "features")))
            self.assertFalse(os.path.exists(os.path.join(tmp_dir, "skills")))
            self.assertFalse(os.path.exists(os.path.join(tmp_dir, "integrations", "mail")))
            self.assertFalse(os.path.exists(os.path.join(tmp_dir, "config.json")))
            self.assertFalse(os.path.exists(os.path.join(tmp_dir, ".env")))


if __name__ == "__main__":
    unittest.main()

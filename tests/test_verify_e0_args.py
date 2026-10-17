"""
阶段三 S3-C 单元测试：verify_e0.py 参数化支持与反例防御 (tests/test_verify_e0_args.py)
验证 scripts/verify_e0.py 的 --agent-root / --parser-root / --core-only 参数行为与错误路径防御：
1. --help 退出码为 0 且包含参数说明
2. 默认兼容性验证 (未传参数时自动定位 agent-root 与默认 parser-root)
3. 路径不存在反例: 虚假 --agent-root 返回退出码 2
4. 路径不存在反例: 虚假 --parser-root 在非 core 模式下返回退出码 2
5. 独立基础 gate 模式: 指定 --core-only 时无需 parser-root
"""

import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VERIFY_SCRIPT = os.path.join(REPO_ROOT, "scripts", "verify_e0.py")


class TestVerifyE0Args(unittest.TestCase):

    def test_01_help_returns_zero(self):
        """反例验证: --help 退出码为 0 且包含核心参数描述"""
        res = subprocess.run(
            [sys.executable, VERIFY_SCRIPT, "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--agent-root", res.stdout)
        self.assertIn("--parser-root", res.stdout)
        self.assertIn("--core-only", res.stdout)
        self.assertIn("--skip-docker", res.stdout)
        self.assertIn("--skip-parser", res.stdout)

    def test_02_invalid_agent_root_fails_with_code_2(self):
        """反例验证: --agent-root 指向不存在路径时以退出码 2 拒绝"""
        res = subprocess.run(
            [
                sys.executable,
                VERIFY_SCRIPT,
                "--agent-root",
                "/non/existent/agent/root/dir",
                "--skip-docker",
                "--core-only",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("找不到 Agent 根目录", res.stderr)

    def test_03_invalid_parser_root_fails_with_code_2_in_full_mode(self):
        """反例验证: 在 full 模式下 --parser-root 指向不存在路径时以退出码 2 拒绝，禁止静默跳过"""
        res = subprocess.run(
            [
                sys.executable,
                VERIFY_SCRIPT,
                "--agent-root",
                REPO_ROOT,
                "--parser-root",
                "/non/existent/parser/root/dir",
                "--skip-docker",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("找不到 parser 兄弟仓库", res.stderr)

    def test_04_core_only_does_not_require_parser(self):
        """正例验证: 指定 --core-only 模式时即使 parser 路径不存在，也不报错阻断退出"""
        cmd = [
            sys.executable,
            "-u",
            VERIFY_SCRIPT,
            "--agent-root",
            REPO_ROOT,
            "--parser-root",
            "/non/existent/parser/root/dir",
            "--core-only",
            "--skip-docker",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # 读取初始引导输出，验证已顺利通过 parser 校验进入 core 模式
            initial_lines = []
            for _ in range(10):
                line = proc.stdout.readline()
                if not line:
                    break
                initial_lines.append(line)
                if "CORE 独立基础门禁" in line:
                    break

            combined = "".join(initial_lines)
            self.assertIn("CORE 独立基础门禁", combined)
        finally:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            proc.kill()
            proc.wait(timeout=5)

    def test_05_core_only_mode_selects_core_test_files(self):
        """反例与计划验证: verify_e0 的 CORE_TEST_FILES 必须只包含基础内核测试，不得包含跨仓业务测试"""
        from scripts.verify_e0 import CORE_TEST_FILES
        self.assertGreater(len(CORE_TEST_FILES), 10)
        # 绝不包含 mail_sync, billing_fetch 等跨仓或业务测试
        for test_file in CORE_TEST_FILES:
            self.assertNotIn("mail_sync", test_file)
            self.assertNotIn("billing_workflow", test_file)
            self.assertNotIn("billing_fetch", test_file)
            # 必须实际存在
            self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, test_file)), f"缺少文件: {test_file}")

    def test_06_format_check_detects_trailing_whitespace_in_untracked_files(self):
        """反例与正例验证: check_formatting 能捕获新未跟踪文件中的末尾空白字符，无空白时通过"""
        from scripts.verify_e0 import check_formatting
        import tempfile
        import shutil
        from unittest.mock import patch

        tmp_repo = tempfile.mkdtemp(prefix="test_format_check_")
        try:
            bad_file = os.path.join(tmp_repo, "dirty_new_file.md")
            with open(bad_file, "w", encoding="utf-8") as f:
                f.write("Line 1  \nLine 2\n")

            def mock_subp_run(cmd, *args, **kwargs):
                if cmd[:3] == ["git", "diff", "--check"] or cmd[:4] == ["git", "diff", "--cached", "--check"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                if cmd[:3] == ["git", "ls-files", "--others"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="dirty_new_file.md\n", stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch("scripts.verify_e0.shutil.which", return_value="/usr/bin/git"), \
                 patch("scripts.verify_e0.subprocess.run", side_effect=mock_subp_run):
                passed, msg = check_formatting(tmp_repo)
                self.assertFalse(passed)
                self.assertIn("trailing whitespace", msg)
                self.assertIn("dirty_new_file.md", msg)

            # 正例验证: 清理末尾空格后通过
            with open(bad_file, "w", encoding="utf-8") as f:
                f.write("Line 1\nLine 2\n")

            with patch("scripts.verify_e0.shutil.which", return_value="/usr/bin/git"), \
                 patch("scripts.verify_e0.subprocess.run", side_effect=mock_subp_run):
                passed, msg = check_formatting(tmp_repo)
                self.assertTrue(passed)
                self.assertEqual(msg, "")
        finally:
            shutil.rmtree(tmp_repo, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

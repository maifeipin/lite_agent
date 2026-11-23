#!/usr/bin/env python3
"""
Web 前端静态资源一键同步与指纹构建脚本测试 (test_sync_web_static.py)。
验证 scripts/sync_web_static.sh 能够镜像同步所有静态目录并正确触发指纹注入。
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestSyncWebStatic(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parent.parent.parent.parent
        self.script_path = self.repo_root / "scripts" / "sync_web_static.sh"
        self.assertTrue(self.script_path.exists(), f"Script not found: {self.script_path}")

    def test_sync_web_static_local_target_success(self):
        """验证执行 sync_web_static.sh 能够正确同步所有静态资源并注入指纹与版本"""
        with tempfile.TemporaryDirectory() as td:
            target_dir = Path(td) / "dist"
            res = subprocess.run(
                ["bash", str(self.script_path), str(target_dir)],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(res.returncode, 0, f"Sync script failed:\n{res.stderr}\n{res.stdout}")
            self.assertIn("前端静态资源同步与指纹注入完成", res.stdout)

            # 校验核心产物完整性
            self.assertTrue((target_dir / "index.html").exists())
            self.assertTrue((target_dir / "main.js").exists())
            self.assertTrue((target_dir / "style.css").exists())
            self.assertTrue((target_dir / "modules").is_dir())
            self.assertTrue((target_dir / "components").is_dir())

            # 校验模块文件是否完整镜像
            self.assertTrue((target_dir / "modules" / "emails.js").exists())
            self.assertTrue((target_dir / "modules" / "todos.js").exists())
            self.assertTrue((target_dir / "components" / "modal.js").exists())

            # 校验 index.html 内容包含指纹与版本标签
            content = (target_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("style.css?v=", content)
            self.assertIn("main.js?v=", content)
            self.assertIn('<meta name="build-sha"', content)
            self.assertIn('id="agent-build-version"', content)

    def test_sync_web_static_fails_on_unwritable_target(self):
        """验证目标目录无写权限时应快速报错退出"""
        with tempfile.TemporaryDirectory() as td:
            target_dir = Path(td) / "readonly_dist"
            target_dir.mkdir()
            # 移除写权限
            os.chmod(target_dir, 0o444)
            try:
                res = subprocess.run(
                    ["bash", str(self.script_path), str(target_dir)],
                    cwd=str(self.repo_root),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertTrue("无写入权限" in res.stderr or "Permission denied" in res.stderr or res.returncode != 0)
            finally:
                os.chmod(target_dir, 0o755)

    def test_sync_web_static_custom_env_sha(self):
        """验证环境变量 LITE_AGENT_BUILD_SHA 能够精确注入到产物中"""
        with tempfile.TemporaryDirectory() as td:
            target_dir = Path(td) / "dist"
            env = os.environ.copy()
            env["LITE_AGENT_BUILD_SHA"] = "cust9999"
            res = subprocess.run(
                ["bash", str(self.script_path), str(target_dir)],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(res.returncode, 0)
            content = (target_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn('<meta name="build-sha" content="cust9999">', content)
            self.assertIn('data-web-sha="cust9999"', content)
            self.assertIn('v1.0.0 (cust9999)', content)

    def test_sync_web_static_remote_self_check_failure_propagation(self):
        """验证远端自检失败时（如 grep SHA 失败或 test 失败）错误能可靠向上传播导致非零退出"""
        with tempfile.TemporaryDirectory() as td:
            mock_bin_dir = Path(td) / "bin"
            mock_bin_dir.mkdir()

            # 创建 mock rsync：模拟上传成功
            mock_rsync = mock_bin_dir / "rsync"
            mock_rsync.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            mock_rsync.chmod(0o755)

            # 创建 mock ssh：模拟远端自检脚本执行（模拟 target 中 index.html 缺指纹，grep 失败）
            mock_ssh = mock_bin_dir / "ssh"
            # 当 ssh 被调用时，如果 remote 命令中的 grep 失败（或者直接让 ssh 模拟远端执行 set -e 后的退出码 1）
            mock_ssh.write_text(
                '#!/bin/sh\n'
                '# 提取最后一个参数作为远程执行的指令\n'
                'for arg do shift; done\n'
                '# 检查是否包含 set -euo pipefail，确保加入了严格错误检测\n'
                'echo "$arg" | grep -q "set -euo pipefail" || { echo "Missing set -euo pipefail" >&2; exit 2; }\n'
                '# 模拟远端校验失败（返回退出码 1）\n'
                'echo "❌ [Remote VPS] 版本自检失败: 未在 \\$TARGET/index.html 中找到构建指纹" >&2\n'
                'exit 1\n',
                encoding="utf-8",
            )
            mock_ssh.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin_dir}:{env.get('PATH', '')}"

            res = subprocess.run(
                ["bash", str(self.script_path), "--remote", "fake_host"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertNotEqual(res.returncode, 0, "远端自检失败时脚本必须以非零退出")
            self.assertIn("版本自检失败", res.stderr)


if __name__ == "__main__":
    unittest.main()

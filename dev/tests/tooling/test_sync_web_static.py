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


if __name__ == "__main__":
    unittest.main()

"""
阶段三 S3-A2 单元测试：干净环境独立运行证明 (tests/tooling/test_clean_core_environment.py)
验证仓外 cwd、独立子进程、干净环境运行纯净 core 基础模式。
"""

import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
VERIFY_SCRIPT = os.path.join(REPO_ROOT, "scripts", "verify_core.py")


class TestCleanCoreEnvironment(unittest.TestCase):

    def test_clean_core_environment_full_lifecycle(self):
        """端到端验证: 仓外 cwd、无 features/skills/Parser 泄漏、假模型回环普通/流式对话、健康检查、鉴权、未启用业务 404 及优雅停机"""
        res = subprocess.run(
            [sys.executable, VERIFY_SCRIPT, "--agent-root", REPO_ROOT],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(res.returncode, 0, f"干净环境独立运行验证失败:\n{res.stderr}\n{res.stdout}")
        self.assertIn("S3-A2 干净环境独立运行证明全部通过", res.stdout)
        self.assertIn("GET /health 与 /api/v1/dashboard 健康状态端点验证通过", res.stdout)
        self.assertIn("鉴权与权限隔离 (401/403/200) 验证通过", res.stdout)
        self.assertIn("未启用业务路由 (404 隔离) 验证通过", res.stdout)
        self.assertIn("普通对话 POST /api/v1/chat 验证通过", res.stdout)
        self.assertIn("流式对话 POST /v1/chat/completions", res.stdout)
        self.assertIn("捕获 SIGINT 并优雅安全退出", res.stdout)


if __name__ == "__main__":
    unittest.main()

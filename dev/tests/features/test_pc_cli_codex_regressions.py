"""Codex resume 通知与 Windows 后台启动环境的回归。"""
import asyncio
import json
import os
import sys

import pytest

from lite_agent.features.pc_cli.daemon import env_probe
from lite_agent.features.pc_cli.daemon.adapters.codex_adapter import CodexCliAdapter


NOTICE = ("This session was recorded with model `gpt-6-luna` but is resuming with "
          "`gpt-6.2-sol`. Consider switching back to `gpt-6-luna` as it may affect Codex performance.")


def test_daemon_does_not_inherit_desktop_runtime_routing(monkeypatch, tmp_path):
    desktop_context = {
        "CODEX_APP_RUNTIME_CLI_PATH": r"C:\WindowsApps\Codex\codex.exe",
        "CODEX_APP_TOOLS_PIPE_PATH": r"\\.\pipe\parent-session",
        "CODEX_WINDOWS_REGISTERED_CORE": "1",
        "CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY": "OpenAI.Codex_parent",
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
    }
    for name, value in desktop_context.items():
        monkeypatch.setenv(name, value)
    # 用户持久设置和权限仍然传给 CLI，且不修改 Daemon 自身的进程环境。
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_PERMISSION_PROFILE", "workspace-write")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    env = env_probe.build_engine_env()
    for name, value in desktop_context.items():
        assert name not in env
        assert os.environ[name] == value
    assert env["CODEX_HOME"] == str(tmp_path)
    assert env["CODEX_PERMISSION_PROFILE"] == "workspace-write"
    assert env["OPENAI_API_KEY"] == "test-placeholder"


@pytest.mark.parametrize("message,exit_code,complete,expected", [
    (NOTICE, 0, True, "completed"),
    ("Authentication failed", 0, True, "failed"),
    (NOTICE, 1, True, "failed"),
    (NOTICE, 0, False, "failed"),
])
def test_exec_notice_does_not_mask_real_failures(tmp_path, message, exit_code, complete, expected):
    events = [{"type": "thread.started", "thread_id": "thread-1"},
              {"type": "error", "message": message},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}]
    if complete:
        events.append({"type": "turn.completed", "usage": {}})
    script = tmp_path / "cli.py"
    script.write_text("import sys\nsys.stdin.read()\n" +
                      "\n".join("print(" + repr(json.dumps(e)) + ", flush=True)" for e in events) +
                      f"\nsys.exit({exit_code})\n", encoding="utf-8")

    class FakeCodex(CodexCliAdapter):
        def build_args(self, **kwargs):
            return [sys.executable, str(script)]

    seen = []

    async def run():
        async def on_event(kind, payload):
            seen.append((kind, payload))
        return await FakeCodex(env=dict(os.environ), turn_timeout_sec=10).run_turn(
            bot_id="bot-1", turn_id="turn-1", cwd=str(tmp_path), prompt="OK",
            native_session_id="thread-1", approval_mode="auto", model="gpt-6.2-sol", on_event=on_event)

    result = asyncio.run(run())
    assert result.status == expected
    assert result.native_session_id == "thread-1"
    assert any(kind == ("warning" if message == NOTICE else "error") for kind, _ in seen)


def test_background_fnm_default_and_sibling_node(tmp_path, monkeypatch):
    default_dir = tmp_path / "fnm" / "aliases" / "default"
    default_dir.mkdir(parents=True)
    node = default_dir / "node.exe"
    node.touch()
    entry = default_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    entry.parent.mkdir(parents=True)
    entry.touch()
    shim = default_dir / "codex.cmd"
    shim.write_text('"%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*', encoding="utf-8")
    monkeypatch.setattr(env_probe.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("FNM_DIR", raising=False)
    env = env_probe.build_engine_env()
    assert str(default_dir) in env["PATH"].split(os.pathsep)
    # 显式 shim 路径也应使用相邻 Node，不能误用 PATH 上的另一版本。
    assert env_probe.build_spawn_argv(str(shim), ["--version"], env, platform="win32") == [
        str(node), str(entry), "--version"]


def test_app_server_resume_passes_model_and_accepts_notice(tmp_path):
    script = tmp_path / "rpc.py"
    script.write_text('''import json, sys
def send(obj):
    print(json.dumps(obj), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method")
    if "id" not in req:
        continue
    result = {}
    if method == "thread/resume":
        assert req["params"]["model"] == "gpt-6.2-sol"
        result = {"thread": {"id": "thread-1"}}
    if method == "turn/start":
        result = {"turn": {"id": "turn-1"}}
    send({"id": req["id"], "result": result})
    if method == "turn/start":
        send({"method": "error", "params": {"message": ''' + repr(NOTICE) + '''}})
        send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "OK"}}})
        send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
''', encoding="utf-8")

    class FakeCodex(CodexCliAdapter):
        def build_app_server_args(self):
            return [sys.executable, str(script)]

    async def run():
        return await FakeCodex(env=dict(os.environ), turn_timeout_sec=10).run_turn(
            bot_id="bot-1", turn_id="turn-1", cwd=str(tmp_path), prompt="OK",
            native_session_id="thread-1", approval_mode="ask", model="gpt-6.2-sol",
            on_event=lambda *args: asyncio.sleep(0))

    result = asyncio.run(run())
    assert result.status == "completed"
    assert result.final_text == "OK"

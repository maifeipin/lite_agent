import sqlite3
import threading

from agent import Agent, IncomingMessage
from session import SessionManager


def _agent_with_sessions(manager: SessionManager) -> Agent:
    agent = Agent.__new__(Agent)
    agent.session_mgr = manager
    agent._session_locks = {}
    agent._session_locks_guard = threading.Lock()
    agent._config = {}
    return agent


def test_reset_archives_old_conversation_and_preserves_history(tmp_path):
    manager = SessionManager(db_path=str(tmp_path / "sessions.db"))
    scope = "wechat:user-1"
    old_key = manager.resolve_active_session(scope)
    manager.set_title(old_key, "旅行线路优化")
    manager.add_message(old_key, "user", "原始问题")
    manager.add_message(old_key, "assistant", "完整回答")

    new_key = manager.reset_session(old_key, scope_key=scope)

    assert new_key != old_key
    assert new_key.startswith(f"{scope}:c_")
    assert manager.resolve_active_session(scope) == new_key
    assert manager.get_or_create(new_key).messages == []

    with sqlite3.connect(manager.db_path) as conn:
        old = conn.execute(
            "SELECT status, title FROM sessions WHERE session_key=?", (old_key,)
        ).fetchone()
        messages = conn.execute(
            "SELECT role, content FROM messages WHERE session_key=? ORDER BY id",
            (old_key,),
        ).fetchall()
    assert old == ("archived", "旅行线路优化")
    assert messages == [("user", "原始问题"), ("assistant", "完整回答")]


def test_new_command_switches_follow_up_messages_to_new_conversation(tmp_path):
    manager = SessionManager(db_path=str(tmp_path / "sessions.db"))
    agent = _agent_with_sessions(manager)
    scope = "api:client-1"
    manager.add_message(scope, "user", "旧会话")

    command = IncomingMessage("api", "client-1", "client-1", "m1", "/new")
    response = agent.handle(command)

    assert response.new_session_key
    assert command.session_key == response.new_session_key
    assert manager.resolve_active_session(scope) == response.new_session_key

    follow_up = IncomingMessage("api", "client-1", "client-1", "m2", "/status")
    agent.handle(follow_up)
    assert follow_up.session_key == response.new_session_key


def test_channel_user_list_uses_stable_scope_after_new(tmp_path):
    manager = SessionManager(db_path=str(tmp_path / "sessions.db"))
    scope = "wecom:owner-id"
    current = manager.resolve_active_session(scope)
    manager.reset_session(current, scope_key=scope)

    assert manager.list_channel_user_ids("wecom") == ["owner-id"]

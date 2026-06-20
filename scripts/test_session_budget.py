"""
Unit tests for session budget optimization (max_history_bytes, max_history, atomic blocks).

Run with: python scripts/test_session_budget.py
"""
import sys
import os
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from session import SessionManager

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}")


def _cleanup_db(path):
    for p in [path, path + "-wal", path + "-shm"]:
        try:
            os.remove(p)
        except OSError:
            pass


def fresh_mgr(max_history=5, max_bytes=200):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return SessionManager(db_path=path, ttl_minutes=30, max_history=max_history, max_history_bytes=max_bytes), path


def test_count_limit():
    print("\n[1] Test: message count limit (max_history)")
    mgr, path = fresh_mgr(max_history=3, max_bytes=1000)
    key = "test:count"
    
    for i in range(5):
        mgr.add_message(key, "user", f"msg_{i}")
        
    history = mgr.get_history(key)
    check("History is truncated by count limit", len(history) == 4)
    check("First item is system prompt", history[0]["role"] == "system")
    check("Contains last 3 messages", [m["content"] for m in history[1:]] == ["msg_2", "msg_3", "msg_4"])
    _cleanup_db(path)


def test_byte_limit():
    print("\n[2] Test: size limit in bytes/chars (max_history_bytes)")
    mgr, path = fresh_mgr(max_history=10, max_bytes=70)  # slightly larger to account for "role" length
    key = "test:bytes"
    
    mgr.add_message(key, "user", "A" * 80) # msg 0
    mgr.add_message(key, "assistant", "B" * 20) # msg 1
    mgr.add_message(key, "user", "C" * 15) # msg 2
    
    history = mgr.get_history(key)
    check("History is truncated by byte limit", len(history) == 3)
    check("First item is system prompt", history[0]["role"] == "system")
    check("Kept newest messages matching budget", history[1]["content"] == "B" * 20 and history[2]["content"] == "C" * 15)
    _cleanup_db(path)


def test_atomic_blocks():
    print("\n[3] Test: atomic blocks (prevent orphan tool messages)")
    mgr, path = fresh_mgr(max_history=10, max_bytes=100)
    key = "test:atomic"
    
    # Ensure the session record exists in the sessions table first
    mgr.get_or_create(key)
    
    with mgr._db_write_lock:
        with mgr._connect() as conn:
            # 0. user
            conn.execute(
                "INSERT INTO messages (session_key, role, content, created_at) VALUES (?, ?, ?, ?)",
                (key, "user", "hello", 1.0)
            )
            # 1. assistant tool_calls (name=9, arguments=19, total=28)
            conn.execute(
                "INSERT INTO messages (session_key, role, content, tool_calls_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, "assistant", "", '[{"id": "call_123", "function": {"name": "get_stock", "arguments": "{\\"symbol\\": \\"AAPL\\"}"}}]', 2.0)
            )
            # 2. tool response (content=18, tool_call_id=8, total=26)
            conn.execute(
                "INSERT INTO messages (session_key, role, content, tool_call_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, "tool", "AAPL price is $180", "call_123", 3.0)
            )
            # 3. assistant text (content=18, total=18)
            conn.execute(
                "INSERT INTO messages (session_key, role, content, created_at) VALUES (?, ?, ?, ?)",
                (key, "assistant", "Here is your stock", 4.0)
            )
    mgr._cache.pop(key, None)
    
    # Cumulative sizes (including role length):
    # msg 3: assistant (9) + content (18) = 27
    # Block tool+assistant: assistant (9) + tool_calls (28) + tool (4) + content (18) + tcid (8) = 67
    # Total = 27 + 67 = 94.
    # Budget = 60: truncates block, leaves msg 3.
    mgr.max_history_bytes = 60
    history = mgr.get_history(key)
    
    # Budget = 100: keeps all.
    mgr.max_history_bytes = 100
    history2 = mgr.get_history(key)
    
    check("Atomic block exceeded budget is truncated safely without orphan tool", len(history) == 2)
    check("Latest message content matches", history[1]["content"] == "Here is your stock")
    check("When budget is sufficient, whole sequence is kept", len(history2) == 4)
    check("Contains assistant tool_calls", "tool_calls" in history2[1])
    check("Contains tool response", history2[2]["role"] == "tool")
    _cleanup_db(path)


def test_working_session_multiplier():
    print("\n[4] Test: working session receives 3x budget space")
    mgr, path = fresh_mgr(max_history=10, max_bytes=60)
    key = "test:working"
    
    mgr.add_message(key, "user", "A" * 20)
    mgr.add_message(key, "assistant", "B" * 20)
    mgr.add_message(key, "user", "C" * 20)
    mgr.add_message(key, "assistant", "D" * 20)
    
    # chatting status -> truncated (approx 110 total chars > 60 budget)
    history_chatting = mgr.get_history(key)
    check("Chatting status triggers truncation", len(history_chatting) < 5)
    
    # working status -> not truncated (110 <= 180 budget)
    mgr.set_goal(key, "test goal")
    history_working = mgr.get_history(key)
    check("Working status gets 3x budget, no truncation", len(history_working) == 4)
    check("Last message matches", history_working[-1]["content"] == "D" * 20)
    _cleanup_db(path)


def test_parallel_tool_calls():
    print("\n[5] Test: parallel tool calls grouping")
    mgr, path = fresh_mgr(max_history=10, max_bytes=150)
    key = "test:parallel"
    
    mgr.get_or_create(key)
    
    with mgr._db_write_lock:
        with mgr._connect() as conn:
            conn.execute(
                "INSERT INTO messages (session_key, role, content, created_at) VALUES (?, ?, ?, ?)",
                (key, "user", "hello", 1.0)
            )
            conn.execute(
                "INSERT INTO messages (session_key, role, content, tool_calls_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, "assistant", "", '[{"id": "call_1", "function": {"name": "get_stock", "arguments": "{\\"symbol\\": \\"AAPL\\"}"}}, {"id": "call_2", "function": {"name": "get_stock", "arguments": "{\\"symbol\\": \\"GOOG\\"}"}}]', 2.0)
            )
            conn.execute(
                "INSERT INTO messages (session_key, role, content, tool_call_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, "tool", "AAPL is 180", "call_1", 3.0)
            )
            conn.execute(
                "INSERT INTO messages (session_key, role, content, tool_call_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, "tool", "GOOG is 170", "call_2", 4.0)
            )
            conn.execute(
                "INSERT INTO messages (session_key, role, content, created_at) VALUES (?, ?, ?, ?)",
                (key, "assistant", "Here are the prices", 5.0)
            )
    mgr._cache.pop(key, None)
    
    mgr.max_history_bytes = 60
    history = mgr.get_history(key)
    
    mgr.max_history_bytes = 200
    history2 = mgr.get_history(key)
    
    check("Parallel tool calls atomic block truncated together", len(history) == 2)
    check("Parallel tool calls atomic block kept together", len(history2) == 5)
    check("Contains first tool response", history2[2]["tool_call_id"] == "call_1")
    check("Contains second tool response", history2[3]["tool_call_id"] == "call_2")
    _cleanup_db(path)


def test_phase3_orphan_cleansing():
    print("\n[6] Test: Phase 3 orphan tool_calls cleansing")
    mgr, path = fresh_mgr(max_history=10, max_bytes=1000)
    key = "test:phase3"
    
    mgr.get_or_create(key)
    
    with mgr._db_write_lock:
        with mgr._connect() as conn:
            conn.execute(
                "INSERT INTO messages (session_key, role, content, tool_calls_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, "assistant", "", '[{"id": "call_999", "function": {"name": "get_weather", "arguments": "{}"}}]', 1.0)
            )
    mgr._cache.pop(key, None)
    
    history = mgr.get_history(key)
    check("History loaded the message", len(history) == 1)
    check("Orphan tool_calls was stripped", "tool_calls" not in history[0])
    _cleanup_db(path)


def main():
    print("=" * 60)
    print("Session budget and atomic blocks unit tests")
    print("=" * 60)
    try:
        test_count_limit()
        test_byte_limit()
        test_atomic_blocks()
        test_working_session_multiplier()
        test_parallel_tool_calls()
        test_phase3_orphan_cleansing()
    except Exception as e:
        print(f"Test crashed: {e}")
        import traceback
        traceback.print_exc()
        global _failed
        _failed += 1

    print("=" * 60)
    print(f"Results: {_passed} passed, {_failed} failed")
    print("=" * 60)
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

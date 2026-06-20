"""
WeCom dedup bug 修复验证。

验证:
1. dedup key 含 user_id + 5min时间窗 + 文本, 不再用纯 text[:80]
2. 同用户同内容同 5min 窗 → 去重 (覆盖企微回调重试)
3. 同用户同内容跨 5min 窗 → 不去重 (不再永久误去重)
4. 不同用户同内容 → 不去重
5. cleanup_expired 清理 24h 前的 processed_msgs

运行: python scripts/test_wecom_dedup.py
"""
import sys
import os
import time
import tempfile
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from session import SessionManager

_passed = 0
_failed = 0

def _cleanup_db(path):
    for p in [path, path + "-wal", path + "-shm"]:
        try: os.remove(p)
        except OSError: pass



def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  pass {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}")


def fresh_mgr():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return SessionManager(db_path=path, ttl_minutes=30, max_history=20), path


def make_wecom_dedup_key(user_id, text, now=None):
    """复刻 wecom.py _feed_message 的 dedup key 生成逻辑"""
    now = now if now is not None else time.time()
    return f"wecom_{user_id}:{int(now // 300)}:{text.strip()[:80]}"


def test_key_structure():
    """1. key 含 user_id + 时间窗 + 文本"""
    print("\n[1] dedup key 结构")
    k = make_wecom_dedup_key("userA", "/check")
    check("key 含 userA", "userA" in k)
    check("key 含 /check 文本", "/check" in k)
    check("key 含时间窗数字", any(c.isdigit() for c in k.split(":")[1]))


def test_same_window_dedup():
    """2. 同用户同内容同 5min 窗 → 去重"""
    print("\n[2] 同 5min 窗去重 (覆盖回调重试)")
    mgr, path = fresh_mgr()
    now = time.time()
    k1 = make_wecom_dedup_key("userA", "/check", now)
    k2 = make_wecom_dedup_key("userA", "/check", now + 10)  # 10s 后 (同窗)
    check("同窗两次 key 相同", k1 == k2)
    check("第一次 is_message_processed=False (新)", mgr.is_message_processed(k1) is False)
    check("第二次 is_message_processed=True (去重)", mgr.is_message_processed(k2) is True)
    _cleanup_db(path)


def test_cross_window_no_dedup():
    """3. 同用户同内容跨 5min 窗 → 不去重 (核心修复)"""
    print("\n[3] 跨 5min 窗不去重 (修复永久误去重)")
    mgr, path = fresh_mgr()
    now = time.time()
    k1 = make_wecom_dedup_key("userA", "/check", now)
    k2 = make_wecom_dedup_key("userA", "/check", now + 301)  # 5min+1s 后 (跨窗)
    check("跨窗两次 key 不同", k1 != k2)
    check("第一次 False", mgr.is_message_processed(k1) is False)
    check("跨窗第二次 False (不再永久去重)", mgr.is_message_processed(k2) is False)
    _cleanup_db(path)


def test_different_user_no_dedup():
    """4. 不同用户同内容同窗 → 不去重"""
    print("\n[4] 不同用户不去重")
    mgr, path = fresh_mgr()
    now = time.time()
    k1 = make_wecom_dedup_key("userA", "/check", now)
    k2 = make_wecom_dedup_key("userB", "/check", now)
    check("不同用户 key 不同", k1 != k2)
    check("userA False", mgr.is_message_processed(k1) is False)
    check("userB False (不同用户不去重)", mgr.is_message_processed(k2) is False)
    _cleanup_db(path)


def test_processed_msgs_cleanup():
    """5. cleanup_expired 清理 24h 前 processed_msgs"""
    print("\n[5] processed_msgs 24h 清理")
    mgr, path = fresh_mgr()
    # 写一条 25h 前的记录
    old_key = "wecom_old_msg_25h"
    with mgr._connect() as conn:
        conn.execute("INSERT INTO processed_msgs (msg_id, created_at) VALUES (?, ?)",
                     (old_key, time.time() - 90000))  # 25h前
        conn.commit()
    # 确认写入
    with mgr._connect() as conn:
        n_before = conn.execute("SELECT COUNT(*) FROM processed_msgs").fetchone()[0]
    check("清理前有 1 条记录", n_before == 1)

    mgr.cleanup_expired()

    with mgr._connect() as conn:
        n_after = conn.execute("SELECT COUNT(*) FROM processed_msgs").fetchone()[0]
        still = conn.execute("SELECT COUNT(*) FROM processed_msgs WHERE msg_id=?", (old_key,)).fetchone()[0]
    check("清理后记录为 0 (25h前已删)", n_after == 0)
    check("old_key 已被清理", still == 0)
    _cleanup_db(path)


def test_recent_processed_msgs_kept():
    """6. cleanup_expired 保留 24h 内的 processed_msgs"""
    print("\n[6] 保留 24h 内记录")
    mgr, path = fresh_mgr()
    recent_key = "wecom_recent_msg"
    mgr.is_message_processed(recent_key)  # 写入当前时间
    mgr.cleanup_expired()
    with mgr._connect() as conn:
        still = conn.execute("SELECT COUNT(*) FROM processed_msgs WHERE msg_id=?", (recent_key,)).fetchone()[0]
    check("近期记录保留", still == 1)
    _cleanup_db(path)


def main():
    print("=" * 60)
    print("WeCom dedup bug 修复验证")
    print("=" * 60)
    test_key_structure()
    test_same_window_dedup()
    test_cross_window_no_dedup()
    test_different_user_no_dedup()
    test_processed_msgs_cleanup()
    test_recent_processed_msgs_kept()
    print("\n" + "=" * 60)
    print(f"result: {_passed} passed, {_failed} failed")
    print("=" * 60)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()

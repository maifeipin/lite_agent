"""
会话管理模块 - SQLite 持久化 + 内存热缓存
支持多会话隔离、目标驱动持续会话、滑动窗口上下文压缩
"""

import sqlite3
import json
import time
import threading
import os
from dataclasses import dataclass, field
from typing import Optional, List, Dict


@dataclass
class Session:
    """单个会话的状态"""
    key: str                                     # "feishu:ou_xxx"
    goal: str = ""                               # AI 提取的当前目标
    status: str = "chatting"                     # chatting | working | done | expired
    messages: List[Dict] = field(default_factory=list)  # [{role, content, ...}]
    tool_calls: int = 0                          # 本轮工具调用计数
    created_at: float = 0.0
    updated_at: float = 0.0
    token_usage: int = 0                         # 累计 token 消耗


class SessionManager:
    """
    会话管理器
    - 复合键 (channel:user_id) 天然隔离不同用户/通道
    - 内存 dict 做热缓存，SQLite 做持久化
    - 滑动窗口控制上下文长度
    """

    def __init__(self, db_path: str = None, ttl_minutes: int = 30, max_history: int = 20):
        if db_path is None:
            base = os.path.dirname(os.path.abspath(__file__))
            db_path = os.path.join(base, "data", "sessions.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        self.db_path = db_path
        self.ttl_seconds = ttl_minutes * 60
        self.max_history = max_history
        self._cache: Dict[str, Session] = {}
        self._lock = threading.RLock()
        self._db_write_lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    #  SQLite 初始化
    # ------------------------------------------------------------------
    def _init_db(self):
        """创建数据库表 (如不存在)"""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_key  TEXT PRIMARY KEY,
                    goal         TEXT DEFAULT '',
                    status       TEXT DEFAULT 'chatting',
                    created_at   REAL,
                    updated_at   REAL,
                    tool_calls   INTEGER DEFAULT 0,
                    token_usage  INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key  TEXT,
                    role         TEXT,
                    content      TEXT,
                    reasoning_content TEXT,
                    tool_call_id TEXT DEFAULT '',
                    name         TEXT DEFAULT '',
                    tool_calls_json TEXT DEFAULT '',
                    created_at   REAL
                );
            """)
            try:
                conn.execute("ALTER TABLE messages ADD COLUMN reasoning_content TEXT")
            except Exception:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_key, created_at)")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS goal_archive (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key  TEXT,
                    goal         TEXT,
                    result       TEXT,
                    steps        INTEGER,
                    finished_at  REAL
                );
                CREATE TABLE IF NOT EXISTS subtask_progress (
                    session_key  TEXT,
                    task_id      TEXT,
                    subtask_dag_json TEXT,
                    status       TEXT DEFAULT 'running',
                    created_at   REAL,
                    updated_at   REAL,
                    PRIMARY KEY (session_key, task_id)
                );
                CREATE TABLE IF NOT EXISTS api_usage_log (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key        TEXT,
                    model              TEXT,
                    prompt_tokens      INTEGER,
                    completion_tokens  INTEGER,
                    total_tokens       INTEGER,
                    created_at         REAL
                );
                CREATE TABLE IF NOT EXISTS processed_msgs (
                    msg_id             TEXT PRIMARY KEY,
                    created_at         REAL
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------------------------------------------------
    #  会话生命周期
    # ------------------------------------------------------------------
    def get_or_create(self, session_key: str) -> Session:
        """获取或创建会话 (先查内存，再查数据库，最后新建)"""
        with self._lock:
            # 1. 内存缓存命中
            if session_key in self._cache:
                return self._cache[session_key]

            # 2. 尝试从 SQLite 恢复
            session = self._restore_from_db(session_key)
            if session:
                self._cache[session_key] = session
                return session

            # 3. 新建
            now = time.time()
            session = Session(
                key=session_key,
                created_at=now,
                updated_at=now,
            )
            self._cache[session_key] = session
            self._persist_session(session)
            return session

    def _restore_from_db(self, session_key: str) -> Optional[Session]:
        """从 SQLite 恢复会话"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT goal, status, created_at, updated_at, tool_calls, token_usage "
                "FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
            if not row:
                return None

            session = Session(
                key=session_key,
                goal=row[0] or "",
                status=row[1] or "chatting",
                created_at=row[2],
                updated_at=row[3],
                tool_calls=row[4] or 0,
                token_usage=row[5] or 0,
            )

            # 恢复消息历史
            msg_rows = conn.execute(
                "SELECT role, content, tool_call_id, name, tool_calls_json, reasoning_content "
                "FROM messages WHERE session_key = ? ORDER BY created_at",
                (session_key,)
            ).fetchall()
            for mr in msg_rows:
                msg = {"role": mr[0], "content": mr[1]}
                if mr[2]:
                    msg["tool_call_id"] = mr[2]
                if mr[3]:
                    msg["name"] = mr[3]
                if mr[4]:
                    try:
                        msg["tool_calls"] = json.loads(mr[4])
                    except json.JSONDecodeError:
                        pass
                if len(mr) > 5 and mr[5]:
                    msg["reasoning_content"] = mr[5]
                session.messages.append(msg)

            return session

    def _persist_session(self, session: Session):
        """将会话元数据写入 SQLite"""
        with self._db_write_lock:
            for attempt in range(3):
                try:
                    with self._connect() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO sessions "
                            "(session_key, goal, status, created_at, updated_at, tool_calls, token_usage) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (session.key, session.goal, session.status,
                             session.created_at, session.updated_at,
                             session.tool_calls, session.token_usage)
                        )
                    return
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower() and attempt < 2:
                        time.sleep(0.1)
                        continue
                    print(f"⚠️ 写入 sessions 失败(数据库被锁): {e}")
                    break

    # ------------------------------------------------------------------
    #  消息与计费管理
    # ------------------------------------------------------------------
    def log_api_usage(self, session_key: str, model: str, prompt_tokens: int, completion_tokens: int, total_tokens: int):
        """记录每一次大模型请求的计费详情 (线程安全)"""
        with self._lock:
            # 1. 更新内存状态并持久化主表
            session = self.get_or_create(session_key)
            session.token_usage += total_tokens
            self._persist_session(session)
            
            # 2. 写入独立的流水表 (带重试防止 WAL 高并发下偶发的 locked)
            for attempt in range(3):
                try:
                    with self._connect() as conn:
                        conn.execute(
                            "INSERT INTO api_usage_log "
                            "(session_key, model, prompt_tokens, completion_tokens, total_tokens, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (session_key, model, prompt_tokens, completion_tokens, total_tokens, time.time())
                        )
                    return
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower() and attempt < 2:
                        time.sleep(0.1)
                        continue
                    print(f"⚠️ 写入 api_usage_log 失败(数据库被锁): {e}")
                    break
                except Exception as e:
                    print(f"⚠️ 写入 api_usage_log 失败: {e}")
                    break

    def add_message(self, session_key: str, role: str, content: str,
                    tool_call_id: str = None, name: str = None,
                    tool_calls_data: list = None,
                    reasoning_content: str = None):
        """添加一条消息到会话 (内存 + SQLite)"""
        with self._lock:
            session = self.get_or_create(session_key)
            msg = {"role": role, "content": content}
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            if name:
                msg["name"] = name
            if tool_calls_data:
                msg["tool_calls"] = tool_calls_data
            if reasoning_content:
                msg["reasoning_content"] = reasoning_content

            session.messages.append(msg)
            session.updated_at = time.time()

            # 持久化消息
            with self._db_write_lock:
                for attempt in range(3):
                    try:
                        with self._connect() as conn:
                            conn.execute(
                                "INSERT INTO messages "
                                "(session_key, role, content, tool_call_id, name, tool_calls_json, reasoning_content, created_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (session_key, role, content,
                                 tool_call_id or "", name or "",
                                 json.dumps(tool_calls_data, ensure_ascii=False) if tool_calls_data else "",
                                 reasoning_content or "",
                                 time.time())
                            )
                        break
                    except sqlite3.OperationalError as e:
                        if "locked" in str(e).lower() and attempt < 2:
                            time.sleep(0.1)
                            continue
                        print(f"⚠️ 写入 messages 失败(数据库被锁): {e}")
                        break
            self._persist_session(session)

    def get_history(self, session_key: str) -> list:
        """
        获取 OpenAI 兼容的消息历史列表
        如果超出 max_history，只保留最新的 N 条
        """
        session = self.get_or_create(session_key)
        messages = session.messages

        if len(messages) <= self.max_history or session.status == "working":
            truncated = list(messages)
        else:
            start_idx = max(0, len(messages) - self.max_history)

            # Phase 1: 寻找安全切割点 (user 或纯文本 assistant)
            while start_idx < len(messages):
                msg = messages[start_idx]
                if msg["role"] == "user":
                    break
                if msg["role"] == "assistant" and "tool_calls" not in msg:
                    break
                start_idx += 1

            if start_idx >= len(messages):
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i]["role"] == "user":
                        start_idx = i
                        break

            truncated = messages[start_idx:]

            # Phase 2: 孤儿 tool 消息检测
            if truncated and truncated[0]["role"] == "tool":
                for i in range(start_idx - 1, -1, -1):
                    if messages[i]["role"] == "assistant" and "tool_calls" in messages[i]:
                        start_idx = i
                        truncated = messages[start_idx:]
                        break
                    elif messages[i]["role"] == "user":
                        break

            if start_idx > 0:
                truncated.insert(0, {
                    "role": "system",
                    "content": f"[系统提示: 之前有 {start_idx} 条早期对话已被压缩省略，以下是最近的对话]"
                })

        # Phase 3: 断电/重启造成的孤儿 tool_calls 清洗
        sanitized = []
        for i, msg in enumerate(truncated):
            if msg["role"] == "assistant" and "tool_calls" in msg:
                # 检查紧跟的下一条消息是不是 tool
                is_broken = False
                if i == len(truncated) - 1:
                    is_broken = True
                elif truncated[i+1]["role"] != "tool":
                    is_broken = True
                
                if is_broken:
                    clean_msg = dict(msg)
                    del clean_msg["tool_calls"]
                    sanitized.append(clean_msg)
                    continue
            sanitized.append(msg)

        return sanitized



    # ------------------------------------------------------------------
    #  目标管理
    # ------------------------------------------------------------------
    def set_goal(self, session_key: str, goal: str):
        """设置当前目标，切换到 working 状态"""
        with self._lock:
            session = self.get_or_create(session_key)
            session.goal = goal
            session.status = "working"
            session.tool_calls = 0
            session.updated_at = time.time()
            self._persist_session(session)

    def mark_done(self, session_key: str, result_summary: str = ""):
        """标记目标完成，归档到 goal_archive，重置状态"""
        with self._lock:
            session = self.get_or_create(session_key)
            if session.goal:
                with self._db_write_lock:
                    for attempt in range(3):
                        try:
                            with self._connect() as conn:
                                conn.execute(
                                    "INSERT INTO goal_archive (session_key, goal, result, steps, finished_at) "
                                    "VALUES (?, ?, ?, ?, ?)",
                                    (session_key, session.goal, result_summary[:500],
                                     session.tool_calls, time.time())
                                )
                            break
                        except sqlite3.OperationalError as e:
                            if "locked" in str(e).lower() and attempt < 2:
                                time.sleep(0.1)
                                continue
                            break
            session.goal = ""
            session.status = "chatting"
            session.tool_calls = 0
            session.updated_at = time.time()
            self._persist_session(session)

    def increment_tool_calls(self, session_key: str) -> int:
        """递增工具调用计数，返回当前值"""
        with self._lock:
            session = self.get_or_create(session_key)
            session.tool_calls += 1
            session.updated_at = time.time()
            return session.tool_calls

    # ------------------------------------------------------------------
    #  会话控制
    # ------------------------------------------------------------------
    def reset_session(self, session_key: str):
        """强制重置会话 (清空消息、重置状态)"""
        with self._lock:
            session = Session(
                key=session_key,
                created_at=time.time(),
                updated_at=time.time(),
            )
            self._cache[session_key] = session
            with self._db_write_lock:
                for attempt in range(3):
                    try:
                        with self._connect() as conn:
                            conn.execute("DELETE FROM messages WHERE session_key = ?", (session_key,))
                        break
                    except sqlite3.OperationalError as e:
                        if "locked" in str(e).lower() and attempt < 2:
                            time.sleep(0.1)
                            continue
                        break
            self._persist_session(session)

    def save_subtask_dag(self, session_key: str, task_id: str,
                          dag_json: str, status: str = "running"):
        """持久化子任务 DAG 进度 (线程安全)"""
        now = time.time()
        with self._db_write_lock:
            for attempt in range(3):
                try:
                    with self._connect() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO subtask_progress "
                            "(session_key, task_id, subtask_dag_json, status, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, COALESCE((SELECT created_at FROM subtask_progress "
                            "WHERE session_key=? AND task_id=?), ?), ?)",
                            (session_key, task_id, dag_json, status, session_key, task_id, now, now)
                        )
                    return
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower() and attempt < 2:
                        time.sleep(0.1)
                        continue
                    print(f"⚠️ 写入 subtask_progress 失败: {e}")
                    break

    def load_subtask_dag(self, session_key: str, task_id: str) -> Optional[tuple]:
        """加载持久化的子任务 DAG, 返回 (dag_json, status) 或 None"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT subtask_dag_json, status FROM subtask_progress "
                "WHERE session_key=? AND task_id=?",
                (session_key, task_id)
            ).fetchone()
            if row:
                return (row[0], row[1])
            return None

    def cleanup_expired(self):
        """清理过期会话 (由外部定时调用)"""
        now = time.time()
        expired_keys = []
        with self._lock:
            for key, session in list(self._cache.items()):
                if session.status == "working":
                    continue
                if now - session.updated_at > self.ttl_seconds:
                    expired_keys.append(key)

            for key in expired_keys:
                session = self._cache.pop(key, None)
                if session and session.goal:
                    self.mark_done(key, "会话超时自动归档")
                with self._db_write_lock:
                    for attempt in range(3):
                        try:
                            with self._connect() as conn:
                                conn.execute("DELETE FROM messages WHERE session_key = ?", (key,))
                                conn.execute("DELETE FROM sessions WHERE session_key = ?", (key,))
                            break
                        except sqlite3.OperationalError as e:
                            if "locked" in str(e).lower() and attempt < 2:
                                time.sleep(0.1)
                                continue
                            break

        if expired_keys:
            print(f"🧹 已清理 {len(expired_keys)} 个过期会话")

    def get_session_info(self, session_key: str) -> dict:
        """获取会话状态摘要 (供 /status 指令使用)"""
        session = self.get_or_create(session_key)
        return {
            "status": session.status,
            "goal": session.goal,
            "message_count": len(session.messages),
            "tool_calls": session.tool_calls,
            "token_usage": session.token_usage,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }

    # ------------------------------------------------------------------
    #  防重放管理
    # ------------------------------------------------------------------
    def is_message_processed(self, msg_id: str) -> bool:
        """检查并记录消息ID（基于 SQLite 的防重放机制）"""
        with self._db_write_lock:
            for attempt in range(3):
                try:
                    with self._connect() as conn:
                        # 尝试插入，如果主键冲突则说明已处理
                        try:
                            conn.execute("INSERT INTO processed_msgs (msg_id, created_at) VALUES (?, ?)", (msg_id, time.time()))
                            return False  # 插入成功，说明是新消息
                        except sqlite3.IntegrityError:
                            return True   # 主键冲突，说明已经处理过
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower() and attempt < 2:
                        time.sleep(0.1)
                        continue
                    return False # 出错时宁可漏过也不要全阻挡
        return False

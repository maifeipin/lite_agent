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
    status: str = "chatting"                     # chatting | working | archived
    messages: List[Dict] = field(default_factory=list)  # [{role, content, ...}]
    tool_calls: int = 0                          # 本轮工具调用计数
    created_at: float = 0.0
    updated_at: float = 0.0
    token_usage: int = 0                         # 累计 token 消耗
    kv_state: Dict = field(default_factory=dict)  # 跨 Skill 共享的中间状态 (OpenRath 状态追踪器)


class SessionManager:
    """
    会话管理器
    - 复合键 (channel:user_id) 天然隔离不同用户/通道
    - 内存 dict 做热缓存，SQLite 做持久化
    - 滑动窗口控制上下文长度
    """

    def __init__(self, db_path: str = None, ttl_minutes: int = 30, max_history: int = 20, max_history_bytes: int = 40000):
        if db_path is None:
            base = os.path.dirname(os.path.abspath(__file__))
            db_path = os.path.join(base, "data", "sessions.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        self.db_path = db_path
        self.ttl_seconds = ttl_minutes * 60
        self.max_history = max_history
        self.max_history_bytes = max_history_bytes
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
            # kv_state: 跨 Skill 共享的中间状态 (JSON), OpenRath 状态追踪器理念
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN kv_state TEXT DEFAULT '{}'")
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
                # 归档会话被重新激活: 重置回 chatting, 刷新 updated_at (跨 TTL 边界对话连续)
                if session.status == "archived":
                    session.status = "chatting"
                    session.updated_at = time.time()
                    self._persist_session(session)
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
                "SELECT goal, status, created_at, updated_at, tool_calls, token_usage, kv_state "
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
            # 恢复 kv_state (JSON)
            try:
                session.kv_state = json.loads(row[6]) if row[6] else {}
            except (json.JSONDecodeError, TypeError):
                session.kv_state = {}

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
                            "(session_key, goal, status, created_at, updated_at, tool_calls, token_usage, kv_state) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (session.key, session.goal, session.status,
                             session.created_at, session.updated_at,
                             session.tool_calls, session.token_usage,
                             json.dumps(session.kv_state, ensure_ascii=False))
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

    def _get_message_size(self, msg: dict) -> int:
        size = 0
        if msg.get("role"):
            size += len(msg["role"])
        if msg.get("content"):
            size += len(msg["content"])
        if msg.get("reasoning_content"):
            size += len(msg["reasoning_content"])
        if msg.get("name"):
            size += len(msg["name"])
        if msg.get("tool_call_id"):
            size += len(msg["tool_call_id"])
        if "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                if isinstance(tc, dict):
                    func = tc.get("function", {})
                    size += len(func.get("name", ""))
                    size += len(func.get("arguments", ""))
                else:
                    size += len(str(tc)[:500])
        return size

    def _group_messages_to_blocks(self, messages: list) -> list:
        # 预先构建 ID 到原始索引的映射，避免在循环中重复构造导致 O(N^2)
        msg_to_index = {id(m): idx for idx, m in enumerate(messages)}
        
        id_to_block = {}
        blocks = []
        
        # 逆序扫描消息以归并同一轮的 tool call 交互
        for msg in reversed(messages):
            role = msg.get("role")
            if role == "tool":
                block = [msg]
                blocks.append(block)
                tcid = msg.get("tool_call_id")
                if tcid:
                    id_to_block[tcid] = block
            elif role == "assistant" and "tool_calls" in msg:
                # 寻找该 assistant 关联的所有已存在的 tool 消息块
                associated_blocks = []
                for tc in msg.get("tool_calls", []):
                    tcid = tc.get("id") if isinstance(tc, dict) else None
                    if tcid and tcid in id_to_block:
                        associated_block = id_to_block[tcid]
                        if associated_block not in associated_blocks:
                            associated_blocks.append(associated_block)
                
                # 合并助理消息与所有相关的工具响应，并从待定块中移除工具块
                new_block = [msg]
                for ab in associated_blocks:
                    new_block.extend(ab)
                    blocks.remove(ab)
                
                # 保持块内消息的原始时序
                new_block.sort(key=lambda m: msg_to_index.get(id(m), 0))
                blocks.append(new_block)
            else:
                # 系统消息、普通对话或无工具调用的助理消息独立成块
                blocks.append([msg])
        
        # 按块内首条消息的原始时序对所有块进行排序
        blocks.sort(key=lambda b: msg_to_index.get(id(b[0]), 0))
        return blocks

    def get_history(self, session_key: str) -> list:
        """
        获取 OpenAI 兼容的消息历史列表
        在 max_history 数量限制之外，额外增加 max_history_bytes 字节预算限制 (对 working 状态会话给予 3 倍预算空间)。
        使用原子块 (atomic blocks) 算法防止孤儿 tool / assistant 消息。
        """
        session = self.get_or_create(session_key)
        messages = session.messages

        if not messages:
            return []

        # 决定当前会话的字节/字符预算 (working 会话放宽 3 倍限制以保留更多任务编排上下文)
        max_bytes = self.max_history_bytes
        if session.status == "working":
            max_bytes = max_bytes * 3

        # 将消息按 tool_calls 完整性划分为原子块
        blocks = self._group_messages_to_blocks(messages)

        selected_blocks = []
        current_size = 0
        current_count = 0

        # 从最新消息往回累加，直到超出预算
        for block in reversed(blocks):
            block_size = sum(self._get_message_size(m) for m in block)
            block_count = len(block)

            # 至少保留最后一个块 (防空)
            if len(selected_blocks) > 0 and (current_size + block_size > max_bytes or current_count + block_count > self.max_history):
                break

            selected_blocks.append(block)
            current_size += block_size
            current_count += block_count

        selected_messages = []
        for block in reversed(selected_blocks):
            selected_messages.extend(block)

        start_idx = len(messages) - len(selected_messages)
        if start_idx > 0:
            selected_messages.insert(0, {
                "role": "system",
                "content": f"[系统提示: 之前有 {start_idx} 条早期对话已因超出容量/字节预算被省略，以下是最近的对话]"
            })

        # Phase 3: 断电/重启造成的孤儿 tool_calls 清洗
        sanitized = []
        for i, msg in enumerate(selected_messages):
            if msg["role"] == "assistant" and "tool_calls" in msg:
                # 检查紧跟的下一条消息是不是 tool
                is_broken = False
                if i == len(selected_messages) - 1:
                    is_broken = True
                elif selected_messages[i+1]["role"] != "tool":
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
    #  跨 Skill 状态共享 (kv_state, OpenRath 状态追踪器理念)
    # ------------------------------------------------------------------
    def set_state(self, session_key: str, key: str, value) -> None:
        """存入一条跨 Skill 共享的中间状态 (如上游 Skill 的计算结果供下游使用)"""
        with self._lock:
            session = self.get_or_create(session_key)
            session.kv_state[key] = value
            session.updated_at = time.time()
            self._persist_session(session)

    def get_state(self, session_key: str, key: str, default=None):
        """读取一条跨 Skill 共享的中间状态"""
        with self._lock:
            session = self.get_or_create(session_key)
            return session.kv_state.get(key, default)

    def del_state(self, session_key: str, key: str) -> bool:
        """删除一条中间状态, 返回是否曾存在"""
        with self._lock:
            session = self.get_or_create(session_key)
            existed = key in session.kv_state
            if existed:
                del session.kv_state[key]
                session.updated_at = time.time()
                self._persist_session(session)
            return existed

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
        """归档过期会话 (由外部定时调用)

        软归档: 不再物理删除 messages, 仅把 session 标记为 archived。
        用户下次发消息时 get_or_create 会恢复并重置回 chatting, 跨 TTL 边界对话记忆连续。
        messages 行由 get_history 的 max_history 滑窗自然截断上下文, 无需为控制上下文而删数据。
        """
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
                # 软归档: 保留 messages, 仅标记状态 (修复跨 TTL 失忆 bug)
                with self._db_write_lock:
                    for attempt in range(3):
                        try:
                            with self._connect() as conn:
                                conn.execute(
                                    "UPDATE sessions SET status='archived' WHERE session_key=?",
                                    (key,)
                                )
                            break
                        except sqlite3.OperationalError as e:
                            if "locked" in str(e).lower() and attempt < 2:
                                time.sleep(0.1)
                                continue
                            break

        if expired_keys:
            print(f"🧹 已归档 {len(expired_keys)} 个过期会话 (消息保留)")

        # 顺带清理 24h 前的防重放记录, 防 processed_msgs 表无限膨胀
        # (feishu/telegram/wecom 每条消息都往里写, wecom 用 5min 时间窗 key 更易累积)
        with self._db_write_lock:
            try:
                with self._connect() as conn:
                    cur = conn.execute(
                        "DELETE FROM processed_msgs WHERE created_at < ?",
                        (now - 86400,)
                    )
                    if cur.rowcount > 0:
                        print(f"🧹 清理 {cur.rowcount} 条过期防重放记录 (>24h)")
            except sqlite3.OperationalError:
                pass  # 锁冲突时跳过, 下次清理

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

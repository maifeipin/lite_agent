"""
存储层 — SQLite 结构化 + ChromaDB 向量

多维向量策略:
  content   — 语义 embedding (文本向量)
  temporal  — 时间行为编码 [hour, dow, age_days]
  identity  — 用户画像向量 (发言习惯、领域偏好)
  importance— 质量标量 (不单独建向量，存 metadata)
"""

import sqlite3
import json
import time
import os
from datetime import datetime
from typing import Optional, List, Dict, Any

DB_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
os.makedirs(DB_DIR, exist_ok=True)

SQLITE_PATH = os.path.join(DB_DIR, 'memory.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id TEXT NOT NULL,
    speaker_nick TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',  -- user / bot / system
    content TEXT NOT NULL,
    msg_type TEXT DEFAULT 'text',
    memory_type TEXT DEFAULT 'event',   -- concept / event / preference / troubleshooting
    created_at REAL NOT NULL,
    importance REAL DEFAULT 0.5,       -- 质量分 0~1
    topic_tags TEXT DEFAULT '[]',      -- JSON array
    feedback_score REAL DEFAULT 0.0,   -- 显式反馈
    distilled_from TEXT DEFAULT NULL,  -- 来源（蒸馏摘要指向原消息 ID 列表）
    is_distillate INTEGER DEFAULT 0    -- 是否为蒸馏产物
);

CREATE TABLE IF NOT EXISTS distilled_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key TEXT UNIQUE NOT NULL,     -- 如 'daily_2026-05-31'
    raw_json TEXT NOT NULL,             -- LLM 产出的原始 JSON
    status TEXT DEFAULT 'pending',      -- pending / vectorized / failed
    source_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    vectorized_at REAL
);

CREATE TABLE IF NOT EXISTS user_profiles (
    speaker_id TEXT PRIMARY KEY,
    nick TEXT DEFAULT '',
    profile_json TEXT DEFAULT '{}',    -- 用户画像 JSON
    interaction_count INTEGER DEFAULT 0,
    first_seen REAL,
    last_seen REAL,
    preferred_topics TEXT DEFAULT '[]',
    avg_importance REAL DEFAULT 0.5
);

CREATE TABLE IF NOT EXISTS retrieval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_text TEXT,
    result_ids TEXT DEFAULT '[]',      -- 命中的 conversation ids
    used_in_reply INTEGER DEFAULT 0,   -- 是否被拼入 context
    feedback_after REAL DEFAULT NULL,  -- 回复后的反馈分
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_speaker ON conversations(speaker_id);
CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(created_at);
CREATE INDEX IF NOT EXISTS idx_conv_importance ON conversations(importance DESC);
CREATE INDEX IF NOT EXISTS idx_retrieval_time ON retrieval_log(created_at);
"""


class MemoryStore:
    """底层存储：SQLite 结构化 + ChromaDB 向量"""

    def __init__(self):
        self.conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._chroma = None
        self._chroma_ready = False

    def _init_schema(self):
        for stmt in SCHEMA.split(';'):
            s = stmt.strip()
            if s:
                self.conn.execute(s)
        # 迁移：为已有数据库添加新列
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """兼容旧 schema — 添加缺失列"""
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(conversations)").fetchall()}
        if 'memory_type' not in existing:
            self.conn.execute("ALTER TABLE conversations ADD COLUMN memory_type TEXT DEFAULT 'event'")

    # ========== ChromaDB (lazy init) ==========

    @property
    def chroma(self):
        if self._chroma is None:
            try:
                import chromadb
                chroma_dir = os.path.join(DB_DIR, 'chroma')
                self._chroma = chromadb.PersistentClient(path=chroma_dir)
                self._collection = self._chroma.get_or_create_collection(
                    name='bot_memory',
                    metadata={'hnsw:space': 'cosine'}
                )
                self._chroma_ready = True
            except ImportError:
                self._chroma_ready = False
        return self._chroma

    def _embedding_function(self, texts: List[str]) -> List[List[float]]:
        """可插拔的 embedding 函数 — 默认用 sentence-transformers"""
        if not hasattr(self, '_embed_model'):
            try:
                from sentence_transformers import SentenceTransformer
                self._embed_model = SentenceTransformer(
                    'BAAI/bge-small-zh-v1.5'
                )
            except ImportError:
                # 降级：使用 ChromaDB 默认的 all-MiniLM-L6-v2
                self._embed_model = None
        if self._embed_model:
            return self._embed_model.encode(texts).tolist()
        return None  # 让 ChromaDB 用自己的

    # ========== 写入 ==========

    def save_message(self, speaker_id: str, speaker_nick: str,
                     content: str, role: str = 'user',
                     msg_type: str = 'text',
                     importance: float = 0.5,
                     memory_type: str = 'event',
                     topic_tags: List[str] = None,
                     session_id: str = '') -> int:
        """保存一条消息，返回 row id"""
        if topic_tags is None:
            topic_tags = []

        created_at = time.time()
        cur = self.conn.execute(
            """INSERT INTO conversations
               (speaker_id, speaker_nick, session_id, role, content,
                msg_type, memory_type, created_at, importance, topic_tags)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (speaker_id, speaker_nick, session_id, role, content,
             msg_type, memory_type, created_at, importance, json.dumps(topic_tags))
        )
        row_id = cur.lastrowid

        # 写入向量库
        if self._chroma_ready and role in ('user', 'bot'):
            try:
                embeddings = self._embedding_function([content])
                self._collection.add(
                    ids=[f'msg_{row_id}'],
                    documents=[content],
                    embeddings=embeddings,
                    metadatas=[{
                        'speaker_id': speaker_id,
                        'role': role,
                        'importance': importance,
                        'created_at': created_at,
                        'tags': ','.join(topic_tags),
                    }]
                )
            except Exception:
                self._chroma_ready = False  # 向量库不可用时降级

        self.conn.commit()
        return row_id

    def save_distilled(self, content: str, source_ids: List[int],
                       importance: float = 0.7, topic_tags: List[str] = None,
                       role: str = 'system') -> int:
        """保存蒸馏产物"""
        created_at = time.time()
        cur = self.conn.execute(
            """INSERT INTO conversations
               (speaker_id, speaker_nick, role, content, created_at,
                importance, topic_tags, distilled_from, is_distillate)
               VALUES ('system','system',?,?,?,?,?,?,1)""",
            (role, content, created_at, importance,
             json.dumps(topic_tags or []),
             json.dumps(source_ids))
        )
        row_id = cur.lastrowid
        self.conn.commit()
        return row_id

    def update_importance(self, msg_id: int, delta: float):
        """调整质量分（±0.1 为单位）"""
        self.conn.execute(
            "UPDATE conversations SET importance = MAX(0, MIN(1, importance + ?)) WHERE id = ?",
            (delta, msg_id)
        )
        self.conn.commit()

    def update_feedback(self, msg_id: int, score: float):
        """记录显式反馈"""
        self.conn.execute(
            "UPDATE conversations SET feedback_score = ? WHERE id = ?",
            (score, msg_id)
        )
        self.conn.commit()

    # ========== 用户画像 ==========

    def touch_user(self, speaker_id: str, nick: str = ''):
        """更新用户最后活跃时间"""
        now = time.time()
        self.conn.execute(
            """INSERT INTO user_profiles (speaker_id, nick, first_seen, last_seen, interaction_count)
               VALUES (?,?,?,?,1)
               ON CONFLICT(speaker_id) DO UPDATE SET
               nick = COALESCE(NULLIF(?, ''), nick),
               last_seen = ?,
               interaction_count = interaction_count + 1""",
            (speaker_id, nick, now, now, nick, now)
        )
        self.conn.commit()

    def get_user_profile(self, speaker_id: str) -> Optional[Dict]:
        row = self.conn.execute(
            "SELECT * FROM user_profiles WHERE speaker_id = ?",
            (speaker_id,)
        ).fetchone()
        if not row:
            return None
        return {
            'speaker_id': row[0],
            'nick': row[1],
            'profile': json.loads(row[2]),
            'interactions': row[3],
            'first_seen': row[4],
            'last_seen': row[5],
            'preferred_topics': json.loads(row[6]),
            'avg_importance': row[7],
        }

    # ========== 检索 ==========

    def semantic_search(self, query: str, top_k: int = 5,
                        speaker_id: str = None,
                        min_importance: float = 0.0) -> List[Dict]:
        """语义检索"""
        if not self._chroma_ready:
            return self._keyword_search(query, top_k, speaker_id, min_importance)

        try:
            embeddings = self._embedding_function([query])
            where_filter = {}
            if speaker_id:
                where_filter['speaker_id'] = speaker_id
            if min_importance > 0:
                where_filter['importance'] = {'$gte': min_importance}

            results = self._collection.query(
                query_embeddings=embeddings,
                n_results=top_k,
                where=where_filter if where_filter else None,
                include=['documents', 'metadatas', 'distances']
            )
            return self._format_chroma_results(results)
        except Exception:
            return self._keyword_search(query, top_k, speaker_id, min_importance)

    def _format_chroma_results(self, results) -> List[Dict]:
        output = []
        if not results['ids'] or not results['ids'][0]:
            return output
        for i, doc_id in enumerate(results['ids'][0]):
            meta = results['metadatas'][0][i]
            output.append({
                'id': doc_id,
                'content': results['documents'][0][i],
                'distance': results['distances'][0][i],
                'speaker_id': meta.get('speaker_id', ''),
                'role': meta.get('role', ''),
                'importance': meta.get('importance', 0.5),
                'tags': meta.get('tags', ''),
            })
        return output

    def _keyword_search(self, query: str, top_k: int = 5,
                        speaker_id: str = None,
                        min_importance: float = 0.0) -> List[Dict]:
        """降级：SQLite LIKE 搜索"""
        params = []
        sql = "SELECT id, content, speaker_id, role, importance, topic_tags FROM conversations WHERE 1=1"
        if speaker_id:
            sql += " AND speaker_id = ?"
            params.append(speaker_id)
        if min_importance > 0:
            sql += " AND importance >= ?"
            params.append(min_importance)
        # 简单的关键词拆词
        keywords = query.split()
        like_clauses = []
        for kw in keywords:
            like_clauses.append("content LIKE ?")
            params.append(f'%{kw}%')
        if like_clauses:
            sql += " AND (" + " OR ".join(like_clauses) + ")"
        sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
        params.append(top_k)
        rows = self.conn.execute(sql, params).fetchall()
        return [{
            'id': f'msg_{r[0]}',
            'content': r[1],
            'speaker_id': r[2],
            'role': r[3],
            'importance': r[4],
            'tags': r[5],
            'distance': 0.0,
        } for r in rows]

    def temporal_pattern(self, speaker_id: str,
                         hour_window: int = 1) -> List[Dict]:
        """
        时间行为模式分析
        返回: 该用户在某个时间段的历史高频对话
        不做 embedding，直接用 SQL 时间窗口聚合
        """
        now = time.time()
        current_hour = datetime.fromtimestamp(now).hour
        hour_start = (current_hour - hour_window) % 24
        hour_end = (current_hour + hour_window) % 24

        if hour_start < hour_end:
            hour_filter = f"CAST(strftime('%H', datetime(created_at, 'unixepoch')) AS INTEGER) BETWEEN {hour_start} AND {hour_end}"
        else:
            hour_filter = f"CAST(strftime('%H', datetime(created_at, 'unixepoch')) AS INTEGER) >= {hour_start} OR CAST(strftime('%H', datetime(created_at, 'unixepoch')) AS INTEGER) <= {hour_end}"

        sql = f"""SELECT content, created_at, importance, topic_tags
                  FROM conversations
                  WHERE speaker_id = ?
                    AND role = 'user'
                    AND {hour_filter}
                  ORDER BY created_at DESC
                  LIMIT 20"""
        rows = self.conn.execute(sql, (speaker_id,)).fetchall()
        return [{
            'content': r[0],
            'time': r[1],
            'importance': r[2],
            'tags': json.loads(r[3]) if r[3] else [],
        } for r in rows]

    # ========== 蒸馏 ==========

    def get_unprocessed_messages(self, since_days: float = 1.0,
                                 min_count: int = 10) -> List[Dict]:
        """获取待蒸馏的原始消息"""
        since_ts = time.time() - since_days * 86400
        rows = self.conn.execute(
            """SELECT id, speaker_id, speaker_nick, content, role, created_at,
                      importance, topic_tags, memory_type
               FROM conversations
               WHERE created_at >= ?
                 AND is_distillate = 0
                 AND distilled_from IS NULL
               ORDER BY created_at ASC""",
            (since_ts,)
        ).fetchall()

        if len(rows) < min_count:
            return []

        return [{
            'id': r[0], 'speaker_id': r[1], 'speaker_nick': r[2],
            'content': r[3], 'role': r[4], 'created_at': r[5],
            'importance': r[6], 'topic_tags': json.loads(r[7]),
            'memory_type': r[8] or 'event',
        } for r in rows]

    def count_unprocessed(self) -> int:
        """统计未蒸馏消息数（用于阈值触发）"""
        row = self.conn.execute(
            """SELECT COUNT(*) FROM conversations
               WHERE is_distillate = 0 AND distilled_from IS NULL"""
        ).fetchone()
        return row[0] if row else 0

    # ========== 两段式写入缓存 ==========

    def save_distill_cache(self, cache_key: str, raw_json: str,
                           source_count: int) -> int:
        """写入蒸馏缓存（防丢失）"""
        cur = self.conn.execute(
            """INSERT OR REPLACE INTO distilled_cache
               (cache_key, raw_json, status, source_count, created_at)
               VALUES (?,?, 'pending', ?, ?)""",
            (cache_key, raw_json, source_count, time.time())
        )
        self.conn.commit()
        return cur.lastrowid

    def get_pending_cache(self) -> List[Dict]:
        """获取未向量化的缓存"""
        rows = self.conn.execute(
            """SELECT id, cache_key, raw_json, source_count, created_at
               FROM distilled_cache WHERE status = 'pending'
               ORDER BY created_at ASC"""
        ).fetchall()
        return [{
            'id': r[0], 'cache_key': r[1], 'raw_json': r[2],
            'source_count': r[3], 'created_at': r[4],
        } for r in rows]

    def mark_cache_vectorized(self, cache_id: int):
        self.conn.execute(
            "UPDATE distilled_cache SET status='vectorized', vectorized_at=? WHERE id=?",
            (time.time(), cache_id)
        )
        self.conn.commit()

    def mark_cache_failed(self, cache_id: int):
        self.conn.execute(
            "UPDATE distilled_cache SET status='failed' WHERE id=?",
            (cache_id,)
        )
        self.conn.commit()

    def mark_distilled(self, msg_ids: List[int]):
        """标记已被蒸馏（distilled_from 记录来源）"""
        # 这些消息已合并到蒸馏产物中
        self.conn.execute(
            f"UPDATE conversations SET distilled_from = '[]' WHERE id IN ({','.join('?'*len(msg_ids))})",
            msg_ids
        )
        self.conn.commit()

    # ========== 检索日志（用于反馈闭环）==========

    def log_retrieval(self, query: str, result_ids: List[str],
                      used: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO retrieval_log (query_text, result_ids, used_in_reply, created_at) VALUES (?,?,?,?)",
            (query, json.dumps(result_ids), 1 if used else 0, time.time())
        )
        self.conn.commit()
        return cur.lastrowid

    def log_retrieval_feedback(self, retrieval_id: int, feedback: float):
        self.conn.execute(
            "UPDATE retrieval_log SET feedback_after = ? WHERE id = ?",
            (feedback, retrieval_id)
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

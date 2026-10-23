"""
用户会话与长期设备凭证服务 (services.user_session).
提供基于 SQLite 的统一用户会话（Session/Access Token）与具备轮换及重用检测的长期设备凭证（Refresh Token）管理。

核心契约（对齐 v1.3.0 基线）：
1. 签发具备独立 scope (user:general) 的用户短效会话 Token 与长期设备 Refresh Token；
2. 依据 remember_me 决定是否签发长期 Refresh Token；
3. 刷新凭证轮换（Rotation）：每次刷新原子吊销原 Refresh Token 并签发新 Refresh Token；
4. 重用检测（Reuse Detection）：当检测到已被轮换/撤销的 Refresh Token 被再次提交，立即吊销关联的整个会话族（Session Family）；
5. 退出登录支持以 access_token 或 refresh_token 定位并撤销整个会话族；
6. 严格遵守架构分层约束：仅依赖标准库，不反向依赖 features 业务层。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lite_agent.core.constants import PROJECT_ROOT

DEFAULT_ACCESS_TTL = 3600               # 1 小时
DEFAULT_REFRESH_TTL = 30 * 24 * 3600    # 30 天
DEFAULT_USER_SCOPE = "user:general"     # 通用用户 API 权限范围


def _sha256(val: str) -> str:
    return hashlib.sha256(val.encode("utf-8")).hexdigest()


class UserSessionService:
    """管理统一用户 Access Token 与长期 Refresh Token 的生命周期与会话族。"""

    def __init__(self, db_path: Optional[str] = None, config: Optional[dict] = None):
        cfg = config or {}
        configured_path = cfg.get("user_session_db") or os.environ.get("USER_SESSION_DB")
        if db_path:
            self.db_path = db_path
        elif configured_path:
            self.db_path = configured_path
        elif os.environ.get("REVIEW_MEETING_DB"):
            self.db_path = os.path.join(
                os.path.dirname(os.path.abspath(os.environ["REVIEW_MEETING_DB"])),
                "user_sessions.db"
            )
        else:
            self.db_path = os.path.join(PROJECT_ROOT, "data", "user_sessions.db")

        self._mem_conn: Optional[sqlite3.Connection] = None
        if self.db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row

        self._ensure_schema()

    @contextmanager
    def _connection(self):
        if self._mem_conn is not None:
            yield self._mem_conn
        else:
            if self.db_path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    def _ensure_schema(self) -> None:
        with self._connection() as conn:
            with conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS user_sessions (
                        token_hash TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        username TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        device_name TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        revoked_at INTEGER,
                        created_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_user_sessions_lookup
                        ON user_sessions(token_hash, expires_at);
                    CREATE INDEX IF NOT EXISTS idx_user_sessions_family
                        ON user_sessions(session_id);

                    CREATE TABLE IF NOT EXISTS user_refresh_tokens (
                        token_hash TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        username TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        device_name TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        revoked_at INTEGER,
                        replaced_by_hash TEXT,
                        created_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_refresh_tokens_lookup
                        ON user_refresh_tokens(token_hash, expires_at);
                    CREATE INDEX IF NOT EXISTS idx_refresh_tokens_family
                        ON user_refresh_tokens(session_id);
                """)

    def is_available(self) -> bool:
        """检查会话服务是否就绪。"""
        try:
            with self._connection() as conn:
                conn.execute("SELECT 1 FROM user_sessions LIMIT 1")
            return True
        except Exception:
            return False

    def create_session(
        self,
        username: str,
        device_id: str = "default",
        device_name: str = "mobile",
        remember_me: bool = True,
        scopes: Optional[List[str]] = None,
        access_ttl: int = DEFAULT_ACCESS_TTL,
        refresh_ttl: int = DEFAULT_REFRESH_TTL,
        *,
        now: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        为用户签发会话。每个会话具备独立 session_id (会话族)。
        若 remember_me 为 True，则同时签发可轮换的 Refresh Token。
        """
        current_time = int(time.time()) if now is None else now
        session_id = uuid.uuid4().hex
        session_token = secrets.token_urlsafe(32)
        access_exp = current_time + access_ttl
        user_scopes = scopes or [DEFAULT_USER_SCOPE]
        scope_str = ",".join(user_scopes)

        refresh_token = None
        refresh_exp = None

        with self._connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO user_sessions (
                        token_hash, session_id, username, device_id, device_name,
                        scope, expires_at, revoked_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (_sha256(session_token), session_id, username, device_id, device_name, scope_str, access_exp, current_time)
                )

                if remember_me:
                    refresh_token = secrets.token_urlsafe(32)
                    refresh_exp = current_time + refresh_ttl
                    conn.execute(
                        """
                        INSERT INTO user_refresh_tokens (
                            token_hash, session_id, username, device_id, device_name,
                            expires_at, revoked_at, replaced_by_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                        """,
                        (_sha256(refresh_token), session_id, username, device_id, device_name, refresh_exp, current_time)
                    )

        result: Dict[str, Any] = {
            "session_token": session_token,
            "access_token": session_token,
            "expires_at": access_exp,
            "access_token_expires_at": access_exp,
            "username": username,
            "session_id": session_id,
            "scopes": user_scopes,
            "device_id": device_id,
            "device_name": device_name,
            "remember_me": remember_me,
        }
        if remember_me and refresh_token and refresh_exp:
            result["refresh_token"] = refresh_token
            result["refresh_expires_at"] = refresh_exp

        return result

    def verify_access_token(
        self,
        token: str,
        required_scope: Optional[str] = DEFAULT_USER_SCOPE,
        *,
        now: Optional[int] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        校验 Access Token 是否有效。
        返回 (session_dict, None) 或 (None, "expired"|"revoked"|"unknown"|"insufficient_scope")。
        """
        if not isinstance(token, str) or len(token) < 16:
            return None, "unknown"

        current_time = int(time.time()) if now is None else now
        token_hash = _sha256(token)

        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT session_id, username, device_id, device_name, scope, expires_at, revoked_at
                    FROM user_sessions
                    WHERE token_hash = ?
                    """,
                    (token_hash,)
                ).fetchone()

                if not row:
                    return None, "unknown"

                if row["revoked_at"] is not None:
                    return None, "revoked"

                if row["expires_at"] <= current_time:
                    return None, "expired"

                scopes = [s.strip() for s in row["scope"].split(",") if s.strip()]
                if required_scope and "*" not in scopes and required_scope not in scopes:
                    return None, "insufficient_scope"

                return {
                    "session_id": row["session_id"],
                    "username": row["username"],
                    "device_id": row["device_id"],
                    "device_name": row["device_name"],
                    "scopes": scopes,
                    "expires_at": row["expires_at"],
                }, None
        except Exception:
            return None, "unknown"

    def refresh_session(
        self,
        refresh_token: str,
        access_ttl: int = DEFAULT_ACCESS_TTL,
        *,
        now: Optional[int] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        使用 Refresh Token 续期，实现刷新凭证轮换（Rotation）与重用检测（Reuse Detection）。
        若检测到已被撤销/轮换的凭证重用，立即撤销整个会话族。
        """
        if not isinstance(refresh_token, str) or len(refresh_token) < 16:
            return None, "invalid"

        current_time = int(time.time()) if now is None else now
        token_hash = _sha256(refresh_token)

        try:
            with self._connection() as conn:
                with conn:
                    row = conn.execute(
                        """
                        SELECT session_id, username, device_id, device_name, expires_at, revoked_at, replaced_by_hash
                        FROM user_refresh_tokens
                        WHERE token_hash = ?
                        """,
                        (token_hash,)
                    ).fetchone()

                    if not row:
                        return None, "invalid"

                    session_id = row["session_id"]
                    username = row["username"]
                    device_id = row["device_id"]
                    device_name = row["device_name"]
                    family_max_expires = row["expires_at"]

                    # 重用检测：已被轮换或吊销的 Refresh Token 被再次使用
                    if row["revoked_at"] is not None:
                        conn.execute(
                            "UPDATE user_sessions SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
                            (current_time, session_id)
                        )
                        conn.execute(
                            "UPDATE user_refresh_tokens SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
                            (current_time, session_id)
                        )
                        return None, "reuse_detected"

                    # 绝对有效期检查
                    if family_max_expires <= current_time:
                        return None, "expired"

                    # 轮换：吊销当前 Refresh Token 并记录替代者
                    new_refresh_token = secrets.token_urlsafe(32)
                    new_refresh_hash = _sha256(new_refresh_token)

                    conn.execute(
                        """
                        UPDATE user_refresh_tokens
                        SET revoked_at = ?, replaced_by_hash = ?
                        WHERE token_hash = ?
                        """,
                        (current_time, new_refresh_hash, token_hash)
                    )

                    # 插入轮换出的新 Refresh Token（继承会话族 session_id 与原绝对过期上限）
                    conn.execute(
                        """
                        INSERT INTO user_refresh_tokens (
                            token_hash, session_id, username, device_id, device_name,
                            expires_at, revoked_at, replaced_by_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                        """,
                        (new_refresh_hash, session_id, username, device_id, device_name, family_max_expires, current_time)
                    )

                    # 吊销该 session_id 之前所有旧 access token
                    conn.execute(
                        "UPDATE user_sessions SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
                        (current_time, session_id)
                    )

                    # 签发新 Access Token
                    new_session_token = secrets.token_urlsafe(32)
                    access_exp = min(current_time + access_ttl, family_max_expires)

                    conn.execute(
                        """
                        INSERT INTO user_sessions (
                            token_hash, session_id, username, device_id, device_name,
                            scope, expires_at, revoked_at, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                        """,
                        (_sha256(new_session_token), session_id, username, device_id, device_name, DEFAULT_USER_SCOPE, access_exp, current_time)
                    )

                    return {
                        "session_token": new_session_token,
                        "access_token": new_session_token,
                        "expires_at": access_exp,
                        "access_token_expires_at": access_exp,
                        "refresh_token": new_refresh_token,
                        "refresh_expires_at": family_max_expires,
                        "session_id": session_id,
                        "username": username,
                        "scopes": [DEFAULT_USER_SCOPE],
                    }, None
        except Exception:
            return None, "error"

    def revoke_session(
        self,
        token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        *,
        now: Optional[int] = None
    ) -> bool:
        """
        支持以 access_token 或 refresh_token 定位并撤销整个会话族（Session Family）。
        """
        current_time = int(time.time()) if now is None else now
        session_id = None

        try:
            with self._connection() as conn:
                with conn:
                    if token and len(token) >= 16:
                        row = conn.execute(
                            "SELECT session_id FROM user_sessions WHERE token_hash = ?",
                            (_sha256(token),)
                        ).fetchone()
                        if row:
                            session_id = row["session_id"]

                    if not session_id and refresh_token and len(refresh_token) >= 16:
                        row = conn.execute(
                            "SELECT session_id FROM user_refresh_tokens WHERE token_hash = ?",
                            (_sha256(refresh_token),)
                        ).fetchone()
                        if row:
                            session_id = row["session_id"]

                    if not session_id:
                        return False

                    conn.execute(
                        "UPDATE user_sessions SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
                        (current_time, session_id)
                    )
                    conn.execute(
                        "UPDATE user_refresh_tokens SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
                        (current_time, session_id)
                    )
            return True
        except Exception:
            return False

    def revoke_all_user_sessions(self, username: str, *, now: Optional[int] = None) -> None:
        """撤销某用户所有设备的所有凭证。"""
        current_time = int(time.time()) if now is None else now
        try:
            with self._connection() as conn:
                with conn:
                    conn.execute(
                        "UPDATE user_sessions SET revoked_at = ? WHERE username = ? AND revoked_at IS NULL",
                        (current_time, username)
                    )
                    conn.execute(
                        "UPDATE user_refresh_tokens SET revoked_at = ? WHERE username = ? AND revoked_at IS NULL",
                        (current_time, username)
                    )
        except Exception:
            pass

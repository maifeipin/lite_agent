"""
PC CLI 知识库收藏服务 (features.pc_cli.favorites).

设计原则：
1. 复用 data/output_archive.db 持久化独立副本（kb_favorites 表），原 PC Bot 删除后收藏与出处快照依然完好；
2. 零 LLM 调用：收藏/追加/取消收藏落盘 SQLite 后实时同步到私有 Meilisearch 索引 personal_kb；
3. Meilisearch 仅作为可随时从 output_archive.db 重建的派生索引，不可用时自动降级 SQLite 查询。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from lite_agent.core.constants import PROJECT_ROOT
from lite_agent.core.sqlite_utils import connect_sqlite

logger = logging.getLogger("features.pc_cli.favorites")

PERSONAL_KB_INDEX = "personal_kb"


def resolve_archive_db_path(config: Optional[Dict[str, Any]] = None) -> str:
    cfg = config if isinstance(config, dict) else {}
    sqlite_cfg = (cfg.get("output_delivery", {}) or {}).get("sqlite", {}) or {}
    raw = str(sqlite_cfg.get("path") or "data/output_archive.db")
    return raw if os.path.isabs(raw) else os.path.join(PROJECT_ROOT, raw)


def make_message_source_key(bot_id: str, turn_id: str, seq_id: int) -> str:
    return f"pc_cli:{bot_id}:{turn_id}:{int(seq_id)}"


def _normalize_tags(raw_tags: Any) -> List[str]:
    if raw_tags is None:
        return []
    if isinstance(raw_tags, str):
        parts = [p.strip() for p in raw_tags.replace("，", ",").split(",")]
    elif isinstance(raw_tags, list):
        parts = [str(p or "").strip() for p in raw_tags]
    else:
        return []
    seen = set()
    out: List[str] = []
    for tag in parts:
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag[:40])
    return out[:20]


def _derive_title(title: str, content: str, items: List[Dict[str, Any]]) -> str:
    cleaned = (title or "").strip()
    if cleaned:
        return cleaned[:120]
    for it in items:
        txt = str(it.get("text") or "").strip()
        if txt:
            first_line = txt.splitlines()[0].lstrip("#*- >").strip()
            if first_line:
                return first_line[:80]
    for line in (content or "").splitlines():
        cand = line.lstrip("#*- >").strip()
        if cand:
            return cand[:80]
    return "PC Bot 收藏"


def resolve_allowed_owners(config: Optional[Dict[str, Any]] = None) -> Set[str]:
    """解析允许访问个人知识库收藏的所有者用户名集合（严格白名单，默认 fail-closed）。"""
    cfg = config if isinstance(config, dict) else {}
    pc_cfg = cfg.get("pc_cli", {}) if isinstance(cfg.get("pc_cli"), dict) else {}
    owners: Set[str] = set()

    raw_list = pc_cfg.get("favorites_owners") or pc_cfg.get("owner_usernames") or cfg.get("owner_usernames")
    if isinstance(raw_list, list):
        owners.update(str(u).strip() for u in raw_list if str(u).strip())
    elif isinstance(raw_list, str):
        owners.update(u.strip() for u in raw_list.replace("，", ",").split(",") if u.strip())

    for single in (
        pc_cfg.get("owner_username"),
        cfg.get("owner_username"),
        os.environ.get("LITE_AGENT_OWNER_USERNAME"),
    ):
        if single and str(single).strip():
            owners.add(str(single).strip())

    # 若未显式配置，仅在与登录端点一致的 dashboard_htpasswd_path 确认“仅存在唯一有效账户”时回退授权；多账户文件严格 fail-closed
    if not owners:
        htpasswd_path = str(cfg.get("dashboard_htpasswd_path") or "/etc/nginx/conf.d/dashboard.htpasswd")
        if htpasswd_path and os.path.isfile(htpasswd_path):
            try:
                found_users: Set[str] = set()
                with open(htpasswd_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and ":" in line:
                            u = line.split(":", 1)[0].strip()
                            if u:
                                found_users.add(u)
                if len(found_users) == 1:
                    owners.update(found_users)
            except Exception:
                pass
    return owners


class PersonalKbMeiliClient:
    """轻量 Meilisearch personal_kb 同步与检索客户端（区分 synced / enqueued / pending，失败自动降级）。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None, index_uid: str = PERSONAL_KB_INDEX):
        cfg = config if isinstance(config, dict) else {}
        meili_cfg = cfg.get("meilisearch", {}) if isinstance(cfg.get("meilisearch"), dict) else {}
        self.base_url = str(meili_cfg.get("url") or "").rstrip("/")
        self.master_key = str(meili_cfg.get("master_key") or "")
        self.index_uid = index_uid
        self._index_initialized = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _request(self, path: str, method: str = "GET", payload: Any = None, timeout: float = 3.0) -> Optional[Any]:
        if not self.enabled:
            return None
        headers = {"Content-Type": "application/json"}
        if self.master_key:
            headers["Authorization"] = f"Bearer {self.master_key}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
                return json.loads(raw) if raw else {}
        except Exception as e:
            logger.debug(f"Meilisearch {method} {path} 降级跳过: {e}")
            return None

    def _wait_task(self, resp: Any, timeout_sec: float = 1.5) -> Tuple[str, Optional[int]]:
        """确认 Meilisearch 异步任务是否真正 succeeded，区分 synced / enqueued / pending。"""
        if resp is None:
            return "pending", None
        if not isinstance(resp, dict):
            return "pending", None
        task_uid = resp.get("taskUid")
        if task_uid is None:
            task_uid = resp.get("uid")
        if task_uid is None:
            return ("synced", None) if resp.get("status") == "succeeded" else ("pending", None)
        try:
            uid_int = int(task_uid)
        except Exception:
            return "pending", None

        deadline = time.monotonic() + max(0.05, float(timeout_sec))
        while time.monotonic() <= deadline:
            tinfo = self._request(f"/tasks/{uid_int}", "GET", timeout=1.5)
            if isinstance(tinfo, dict):
                st = tinfo.get("status")
                if st == "succeeded":
                    return "synced", uid_int
                if st in ("failed", "canceled"):
                    logger.warning(f"Meilisearch task {uid_int} failed: {tinfo.get('error')}")
                    return "pending", uid_int
            time.sleep(0.05)
        return "enqueued", uid_int

    def ensure_index(self) -> bool:
        if not self.enabled:
            return False
        if self._index_initialized:
            return True
        with self._lock:
            if self._index_initialized:
                return True
            created = self._request("/indexes", "POST", {"uid": self.index_uid, "primaryKey": "id"})
            if created is None:
                return False
            self._wait_task(created, timeout_sec=1.0)
            settings_resp = self._request(
                f"/indexes/{self.index_uid}/settings",
                "PATCH",
                {
                    "searchableAttributes": ["title", "content", "note", "tags", "bot_name", "cwd"],
                    "filterableAttributes": ["source_type", "bot_id", "tags"],
                    "sortableAttributes": ["updated_at", "created_at"],
                },
            )
            self._wait_task(settings_resp, timeout_sec=1.0)
            self._index_initialized = True
            return True

    def upsert_document(self, doc: Dict[str, Any]) -> Tuple[str, Optional[int]]:
        if not self.ensure_index():
            return "pending", None
        resp = self._request(f"/indexes/{self.index_uid}/documents", "POST", [doc])
        return self._wait_task(resp)

    def delete_document(self, doc_id: str) -> Tuple[str, Optional[int]]:
        if not self.enabled:
            return "pending", None
        resp = self._request(f"/indexes/{self.index_uid}/documents/{doc_id}", "DELETE")
        return self._wait_task(resp)

    def rebuild_documents(self, docs: List[Dict[str, Any]]) -> Tuple[str, Optional[int]]:
        if not self.ensure_index():
            return "pending", None
        del_resp = self._request(f"/indexes/{self.index_uid}/documents", "DELETE")
        del_state, del_uid = self._wait_task(del_resp, timeout_sec=3.0)
        if del_state == "pending":
            return "pending", del_uid
        if not docs:
            return del_state, del_uid
        add_resp = self._request(f"/indexes/{self.index_uid}/documents", "POST", docs, timeout=10.0)
        return self._wait_task(add_resp, timeout_sec=5.0)

    def search(
        self,
        query: str,
        *,
        bot_id: Optional[str] = None,
        source_type: Optional[str] = None,
        limit: int = 30,
    ) -> Optional[List[Dict[str, Any]]]:
        if not self.enabled:
            return None
        filters: List[str] = []
        if bot_id:
            safe_bot = bot_id.replace('"', '\\"')
            filters.append(f'bot_id = "{safe_bot}"')
        if source_type:
            safe_src = source_type.replace('"', '\\"')
            filters.append(f'source_type = "{safe_src}"')
        body: Dict[str, Any] = {
            "q": query,
            "limit": max(1, min(int(limit), 100)),
            "attributesToCrop": ["content"],
            "cropLength": 140,
        }
        if filters:
            body["filter"] = " AND ".join(filters)
        res = self._request(f"/indexes/{self.index_uid}/search", "POST", body)
        if not isinstance(res, dict) or not isinstance(res.get("hits"), list):
            return None
        return res["hits"]


class PcCliFavoritesService:
    """PC Bots 收藏到知识库核心服务（SQLite 权威源库 + Meilisearch 实时派生索引）。"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        meili_client: Optional[PersonalKbMeiliClient] = None,
        allowed_owners: Optional[Set[str]] = None,
    ):
        self.db_path = db_path or resolve_archive_db_path(config)
        self.meili = meili_client if meili_client is not None else PersonalKbMeiliClient(config)
        self.allowed_owners: Set[str] = set(allowed_owners) if allowed_owners is not None else resolve_allowed_owners(config)
        self._lock = threading.RLock()
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = connect_sqlite(self.db_path, timeout=10.0, row_factory=sqlite3.Row, close_on_exit=True)
        if not self._schema_ready:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_favorites (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL DEFAULT 'pc_cli',
                    source_key TEXT NOT NULL UNIQUE,
                    bot_id TEXT NOT NULL DEFAULT '',
                    bot_name TEXT NOT NULL DEFAULT '',
                    node_id TEXT NOT NULL DEFAULT '',
                    cwd TEXT NOT NULL DEFAULT '',
                    turn_id TEXT NOT NULL DEFAULT '',
                    seq_id INTEGER NOT NULL DEFAULT 0,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    sync_state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(kb_favorites)").fetchall()}
            if "sync_state" not in cols:
                conn.execute("ALTER TABLE kb_favorites ADD COLUMN sync_state TEXT NOT NULL DEFAULT 'pending'")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_favorites_pending_deletes (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_fav_bot ON kb_favorites(bot_id, updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_fav_updated ON kb_favorites(updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_fav_sync ON kb_favorites(sync_state)")
            self._schema_ready = True
        return conn

    def _update_sync_state(self, fav_id: str, sync_state: str) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute("UPDATE kb_favorites SET sync_state = ? WHERE id = ?", (sync_state, fav_id))

    def _normalize_sync_result(self, raw_result: Any) -> str:
        if isinstance(raw_result, tuple) and raw_result:
            st = str(raw_result[0] or "pending")
            return st if st in ("synced", "enqueued", "pending") else "pending"
        return "synced" if raw_result else "pending"

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["tags"] = json.loads(d.pop("tags_json", "[]") or "[]")
        except Exception:
            d["tags"] = []
        try:
            d["source_refs"] = json.loads(d.pop("source_refs_json", "[]") or "[]")
        except Exception:
            d["source_refs"] = []
        return d

    @staticmethod
    def _to_meili_doc(fav: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": fav["id"],
            "source_type": fav.get("source_type", "pc_cli"),
            "source_key": fav.get("source_key", ""),
            "bot_id": fav.get("bot_id", ""),
            "bot_name": fav.get("bot_name", ""),
            "node_id": fav.get("node_id", ""),
            "cwd": fav.get("cwd", ""),
            "turn_id": fav.get("turn_id", ""),
            "seq_id": int(fav.get("seq_id") or 0),
            "title": fav.get("title", ""),
            "content": fav.get("content", ""),
            "note": fav.get("note", ""),
            "tags": fav.get("tags") or [],
            "created_at": fav.get("created_at", ""),
            "updated_at": fav.get("updated_at", ""),
        }

    def get_favorite(self, fav_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM kb_favorites WHERE id = ?", (fav_id,)).fetchone()
                return self._row_to_dict(row) if row else None

    def list_collected_source_keys(self, bot_id: Optional[str] = None) -> List[str]:
        keys = set()
        with self._lock:
            with self._connect() as conn:
                if bot_id:
                    rows = conn.execute(
                        "SELECT source_key, source_refs_json FROM kb_favorites WHERE bot_id = ?",
                        (bot_id,),
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT source_key, source_refs_json FROM kb_favorites").fetchall()
        for row in rows:
            if row["source_key"]:
                keys.add(row["source_key"])
            try:
                refs = json.loads(row["source_refs_json"] or "[]")
                for ref in refs:
                    sk = ref.get("source_key") if isinstance(ref, dict) else None
                    if sk:
                        keys.add(sk)
            except Exception:
                pass
        return sorted(keys)

    def save_favorite(
        self,
        *,
        bot_id: str,
        bot_name: str = "",
        node_id: str = "",
        cwd: str = "",
        title: str = "",
        content: str = "",
        note: str = "",
        tags: Any = None,
        items: Optional[List[Dict[str, Any]]] = None,
        append_to_id: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_bot_id = str(bot_id or "").strip()
        raw_items = items if isinstance(items, list) else []

        norm_items: List[Dict[str, Any]] = []
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            tid = str(it.get("turn_id") or "").strip()
            try:
                sid = int(it.get("seq_id") or 0)
            except Exception:
                sid = 0
            role = str(it.get("role") or "assistant").strip()
            txt = str(it.get("text") or "").strip()
            sk = str(it.get("source_key") or "").strip() or make_message_source_key(clean_bot_id, tid, sid)
            norm_items.append(
                {
                    "source_key": sk,
                    "bot_id": clean_bot_id,
                    "turn_id": tid,
                    "seq_id": sid,
                    "role": role,
                    "text": txt,
                    "collected_at": now_iso,
                }
            )

        clean_content = str(content or "").strip()
        if not clean_content and norm_items:
            parts = []
            for it in norm_items:
                if not it["text"]:
                    continue
                prefix = "**用户提问：**\n" if it["role"] == "user" else ""
                parts.append(f"{prefix}{it['text']}")
            clean_content = "\n\n".join(parts).strip()

        if not clean_content:
            return False, {"error": "收藏正文不能为空"}

        clean_tags = _normalize_tags(tags)
        clean_note = str(note or "").strip()
        clean_append_id = str(append_to_id or "").strip()

        appended_fav: Optional[Dict[str, Any]] = None
        with self._lock:
            with self._connect() as conn:
                # 1. 追加到已有收藏卡片（按 source_key 去重，避免同一消息重复追加）
                if clean_append_id:
                    row = conn.execute("SELECT * FROM kb_favorites WHERE id = ?", (clean_append_id,)).fetchone()
                    if not row:
                        return False, {"error": f"目标收藏卡片不存在: {clean_append_id}"}
                    existing = self._row_to_dict(row)
                    existing_refs = list(existing.get("source_refs") or [])
                    existing_keys = {
                        r.get("source_key") for r in existing_refs if isinstance(r, dict) and r.get("source_key")
                    }
                    if existing.get("source_key"):
                        existing_keys.add(existing["source_key"])

                    new_refs = [it for it in norm_items if it["source_key"] not in existing_keys]
                    if norm_items and not new_refs:
                        return True, {
                            "favorite": existing,
                            "deduplicated": True,
                            "appended": False,
                            "sync_state": existing.get("sync_state", "synced"),
                            "meili_synced": existing.get("sync_state") == "synced",
                        }

                    # 若用户未手工修改正文且只剩部分未收藏项，则仅追加增量项；否则追加用户编辑后的正文
                    append_block = clean_content
                    if new_refs and len(new_refs) < len(norm_items) and not content.strip():
                        append_block = "\n\n".join(
                            (("**用户提问：**\n" if it["role"] == "user" else "") + it["text"])
                            for it in new_refs
                            if it["text"]
                        ).strip()

                    merged_content = f"{existing['content'].rstrip()}\n\n---\n\n{append_block}"
                    merged_tags = _normalize_tags(list(existing.get("tags") or []) + clean_tags)
                    merged_note = existing.get("note") or ""
                    if clean_note:
                        merged_note = f"{merged_note}\n{clean_note}".strip() if merged_note else clean_note
                    merged_refs = existing_refs + [
                        {k: v for k, v in it.items() if k != "text"} for it in (new_refs or norm_items)
                    ]

                    conn.execute(
                        """
                        UPDATE kb_favorites
                        SET content = ?, note = ?, tags_json = ?, source_refs_json = ?, sync_state = 'pending', updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            merged_content,
                            merged_note,
                            json.dumps(merged_tags, ensure_ascii=False),
                            json.dumps(merged_refs, ensure_ascii=False),
                            now_iso,
                            clean_append_id,
                        ),
                    )
                    updated_row = conn.execute("SELECT * FROM kb_favorites WHERE id = ?", (clean_append_id,)).fetchone()
                    appended_fav = self._row_to_dict(updated_row)

        if appended_fav is not None:
            sync_state = self._normalize_sync_result(self.meili.upsert_document(self._to_meili_doc(appended_fav)))
            self._update_sync_state(appended_fav["id"], sync_state)
            appended_fav["sync_state"] = sync_state
            return True, {
                "favorite": appended_fav,
                "deduplicated": False,
                "appended": True,
                "sync_state": sync_state,
                "meili_synced": sync_state == "synced",
            }

        with self._lock:
            with self._connect() as conn:
                # 2. 新建收藏卡片（单条消息严格按 source_key 幂等去重；多条消息按排序集合哈希去重）
                if len(norm_items) == 1:
                    primary_key = norm_items[0]["source_key"]
                    primary_turn = norm_items[0]["turn_id"]
                    primary_seq = norm_items[0]["seq_id"]
                elif len(norm_items) > 1:
                    joined = "|".join(sorted(it["source_key"] for it in norm_items))
                    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
                    primary_key = f"pc_cli:{clean_bot_id}:batch:{digest}"
                    primary_turn = norm_items[-1]["turn_id"]
                    primary_seq = norm_items[-1]["seq_id"]
                else:
                    digest = hashlib.sha256(f"{clean_bot_id}:{clean_content}".encode("utf-8")).hexdigest()[:16]
                    primary_key = f"pc_cli:{clean_bot_id}:manual:{digest}"
                    primary_turn = ""
                    primary_seq = 0

                dup_row = conn.execute("SELECT * FROM kb_favorites WHERE source_key = ?", (primary_key,)).fetchone()
                if dup_row:
                    return True, {
                        "favorite": self._row_to_dict(dup_row),
                        "deduplicated": True,
                        "appended": False,
                        "meili_synced": True,
                    }

                fav_id = f"fav_{uuid.uuid4().hex[:16]}"
                final_title = _derive_title(title, clean_content, norm_items)
                stored_refs = [{k: v for k, v in it.items() if k != "text"} for it in norm_items]
                if not stored_refs:
                    stored_refs = [
                        {
                            "source_key": primary_key,
                            "bot_id": clean_bot_id,
                            "turn_id": primary_turn,
                            "seq_id": primary_seq,
                            "role": "assistant",
                            "collected_at": now_iso,
                        }
                    ]

                conn.execute(
                    """
                    INSERT INTO kb_favorites(
                        id, source_type, source_key, bot_id, bot_name, node_id, cwd,
                        turn_id, seq_id, title, content, note, tags_json, source_refs_json,
                        created_at, updated_at
                    ) VALUES (?, 'pc_cli', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fav_id,
                        primary_key,
                        clean_bot_id,
                        str(bot_name or "").strip(),
                        str(node_id or "").strip(),
                        str(cwd or "").strip(),
                        primary_turn,
                        int(primary_seq or 0),
                        final_title,
                        clean_content,
                        clean_note,
                        json.dumps(clean_tags, ensure_ascii=False),
                        json.dumps(stored_refs, ensure_ascii=False),
                        now_iso,
                        now_iso,
                    ),
                )
                row = conn.execute("SELECT * FROM kb_favorites WHERE id = ?", (fav_id,)).fetchone()
                fav = self._row_to_dict(row)

        sync_state = self._normalize_sync_result(self.meili.upsert_document(self._to_meili_doc(fav)))
        self._update_sync_state(fav["id"], sync_state)
        fav["sync_state"] = sync_state
        return True, {
            "favorite": fav,
            "deduplicated": False,
            "appended": False,
            "sync_state": sync_state,
            "meili_synced": sync_state == "synced",
        }

    def delete_favorite(self, fav_id: str) -> Tuple[bool, Dict[str, Any]]:
        clean_id = str(fav_id or "").strip()
        if not clean_id:
            return False, {"error": "缺少收藏 ID"}
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM kb_favorites WHERE id = ?", (clean_id,))
                deleted = cur.rowcount > 0
                if deleted:
                    conn.execute(
                        "INSERT OR REPLACE INTO kb_favorites_pending_deletes(id, created_at) VALUES (?, ?)",
                        (clean_id, now_iso),
                    )
        if not deleted:
            return False, {"error": "收藏条目不存在"}
        sync_state = self._normalize_sync_result(self.meili.delete_document(clean_id))
        if sync_state == "synced":
            with self._lock:
                with self._connect() as conn:
                    conn.execute("DELETE FROM kb_favorites_pending_deletes WHERE id = ?", (clean_id,))
        return True, {"deleted": True, "id": clean_id, "sync_state": sync_state, "meili_synced": sync_state == "synced"}

    def _query_sqlite_favorites(
        self,
        *,
        q: str = "",
        bot_id: Optional[str] = None,
        source_type: Optional[str] = None,
        only_unsynced: bool = False,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if bot_id:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if source_type:
            clauses.append("source_type = ?")
            params.append(source_type)
        if only_unsynced:
            clauses.append("sync_state != 'synced'")
        if q:
            like = f"%{q}%"
            clauses.append("(title LIKE ? OR content LIKE ? OR note LIKE ? OR tags_json LIKE ? OR bot_name LIKE ?)")
            params.extend([like, like, like, like, like])
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM kb_favorites {where_sql} ORDER BY updated_at DESC LIMIT ?",
                    tuple(params),
                ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def search_favorites(
        self,
        *,
        query: str = "",
        bot_id: Optional[str] = None,
        source_type: Optional[str] = None,
        limit: int = 30,
    ) -> Dict[str, Any]:
        q = str(query or "").strip()
        safe_limit = max(1, min(int(limit or 30), 100))

        # 有关键词时优先走 Meilisearch personal_kb；若存在未追平项（sync_state != 'synced'），自动从 SQLite 补查并合并，绝不漏搜
        if q:
            hits = self.meili.search(q, bot_id=bot_id, source_type=source_type, limit=safe_limit)
            if hits is not None:
                hit_ids = [str(h.get("id") or "") for h in hits if h.get("id")]
                by_id: Dict[str, Dict[str, Any]] = {}
                if hit_ids:
                    placeholders = ",".join("?" for _ in hit_ids)
                    with self._lock:
                        with self._connect() as conn:
                            rows = conn.execute(
                                f"SELECT * FROM kb_favorites WHERE id IN ({placeholders})",
                                tuple(hit_ids),
                            ).fetchall()
                    by_id = {r["id"]: self._row_to_dict(r) for r in rows}
                ordered: List[Dict[str, Any]] = []
                seen_ids: Set[str] = set()
                for h in hits:
                    hid = str(h.get("id") or "")
                    item = by_id.get(hid)
                    if not item or hid in seen_ids:
                        continue
                    formatted = h.get("_formatted") if isinstance(h.get("_formatted"), dict) else {}
                    if formatted.get("content"):
                        item["snippet"] = formatted["content"]
                    ordered.append(item)
                    seen_ids.add(hid)

                # 补查尚未确认写入 Meilisearch 的本地收藏条目
                unsynced_matches = self._query_sqlite_favorites(
                    q=q, bot_id=bot_id, source_type=source_type, only_unsynced=True, limit=safe_limit
                )
                supplemented = False
                for uitem in unsynced_matches:
                    if uitem["id"] not in seen_ids:
                        ordered.append(uitem)
                        seen_ids.add(uitem["id"])
                        supplemented = True

                return {
                    "favorites": ordered[:safe_limit],
                    "engine": "meilisearch+sqlite" if supplemented else "meilisearch",
                }

        # 无关键词列表或 Meilisearch 不可用：直接查 SQLite 权威库
        return {
            "favorites": self._query_sqlite_favorites(
                q=q, bot_id=bot_id, source_type=source_type, only_unsynced=False, limit=safe_limit
            ),
            "engine": "sqlite",
        }

    def rebuild_meili_index(self) -> Tuple[bool, Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM kb_favorites ORDER BY updated_at DESC").fetchall()
                del_rows = conn.execute("SELECT id, created_at FROM kb_favorites_pending_deletes").fetchall()
        snapshot_favs = [self._row_to_dict(r) for r in rows]
        snapshot_versions = [(f["id"], f["updated_at"]) for f in snapshot_favs]
        snapshot_pending_deletes = [(str(r["id"]), str(r["created_at"])) for r in del_rows]
        docs = [self._to_meili_doc(f) for f in snapshot_favs]

        sync_state = self._normalize_sync_result(self.meili.rebuild_documents(docs))
        if sync_state in ("synced", "enqueued"):
            with self._lock:
                with self._connect() as conn:
                    for fid, f_updated in snapshot_versions:
                        conn.execute(
                            "UPDATE kb_favorites SET sync_state = ? WHERE id = ? AND updated_at = ?",
                            (sync_state, fid, f_updated),
                        )
                    if sync_state == "synced":
                        for did, d_created in snapshot_pending_deletes:
                            conn.execute(
                                "DELETE FROM kb_favorites_pending_deletes WHERE id = ? AND created_at = ?",
                                (did, d_created),
                            )
        return sync_state == "synced", {
            "rebuilt_count": len(docs),
            "sync_state": sync_state,
            "meili_synced": sync_state == "synced",
        }

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
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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


class PersonalKbMeiliClient:
    """轻量 Meilisearch personal_kb 同步与检索客户端（失败自动降级，绝不阻塞源库落盘）。"""

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
            self._request(
                f"/indexes/{self.index_uid}/settings",
                "PATCH",
                {
                    "searchableAttributes": ["title", "content", "note", "tags", "bot_name", "cwd"],
                    "filterableAttributes": ["source_type", "bot_id", "tags"],
                    "sortableAttributes": ["updated_at", "created_at"],
                },
            )
            self._index_initialized = True
            return True

    def upsert_document(self, doc: Dict[str, Any]) -> bool:
        if not self.ensure_index():
            return False
        return self._request(f"/indexes/{self.index_uid}/documents", "POST", [doc]) is not None

    def delete_document(self, doc_id: str) -> bool:
        if not self.enabled:
            return False
        return self._request(f"/indexes/{self.index_uid}/documents/{doc_id}", "DELETE") is not None

    def rebuild_documents(self, docs: List[Dict[str, Any]]) -> bool:
        if not self.ensure_index():
            return False
        if self._request(f"/indexes/{self.index_uid}/documents", "DELETE") is None:
            return False
        if not docs:
            return True
        return self._request(f"/indexes/{self.index_uid}/documents", "POST", docs, timeout=10.0) is not None

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
    ):
        self.db_path = db_path or resolve_archive_db_path(config)
        self.meili = meili_client if meili_client is not None else PersonalKbMeiliClient(config)
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_fav_bot ON kb_favorites(bot_id, updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_fav_updated ON kb_favorites(updated_at DESC)")
            self._schema_ready = True
        return conn

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
                            "meili_synced": True,
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
                        SET content = ?, note = ?, tags_json = ?, source_refs_json = ?, updated_at = ?
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
                    fav = self._row_to_dict(updated_row)
                    synced = self.meili.upsert_document(self._to_meili_doc(fav))
                    return True, {
                        "favorite": fav,
                        "deduplicated": False,
                        "appended": True,
                        "meili_synced": synced,
                    }

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

        synced = self.meili.upsert_document(self._to_meili_doc(fav))
        return True, {
            "favorite": fav,
            "deduplicated": False,
            "appended": False,
            "meili_synced": synced,
        }

    def delete_favorite(self, fav_id: str) -> Tuple[bool, Dict[str, Any]]:
        clean_id = str(fav_id or "").strip()
        if not clean_id:
            return False, {"error": "缺少收藏 ID"}
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM kb_favorites WHERE id = ?", (clean_id,))
                deleted = cur.rowcount > 0
        if not deleted:
            return False, {"error": "收藏条目不存在"}
        synced = self.meili.delete_document(clean_id)
        return True, {"deleted": True, "id": clean_id, "meili_synced": synced}

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

        # 有关键词时优先走 Meilisearch personal_kb；不可用时自动降级 SQLite LIKE
        if q:
            hits = self.meili.search(q, bot_id=bot_id, source_type=source_type, limit=safe_limit)
            if hits is not None:
                hit_ids = [str(h.get("id") or "") for h in hits if h.get("id")]
                if not hit_ids:
                    return {"favorites": [], "engine": "meilisearch"}
                placeholders = ",".join("?" for _ in hit_ids)
                with self._lock:
                    with self._connect() as conn:
                        rows = conn.execute(
                            f"SELECT * FROM kb_favorites WHERE id IN ({placeholders})",
                            tuple(hit_ids),
                        ).fetchall()
                by_id = {r["id"]: self._row_to_dict(r) for r in rows}
                ordered: List[Dict[str, Any]] = []
                for h in hits:
                    hid = str(h.get("id") or "")
                    item = by_id.get(hid)
                    if not item:
                        continue
                    formatted = h.get("_formatted") if isinstance(h.get("_formatted"), dict) else {}
                    if formatted.get("content"):
                        item["snippet"] = formatted["content"]
                    ordered.append(item)
                return {"favorites": ordered, "engine": "meilisearch"}

        # 无关键词列表或 Meilisearch 降级：直接查 SQLite 权威库
        clauses: List[str] = []
        params: List[Any] = []
        if bot_id:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if source_type:
            clauses.append("source_type = ?")
            params.append(source_type)
        if q:
            like = f"%{q}%"
            clauses.append("(title LIKE ? OR content LIKE ? OR note LIKE ? OR tags_json LIKE ? OR bot_name LIKE ?)")
            params.extend([like, like, like, like, like])
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(safe_limit)
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM kb_favorites {where_sql} ORDER BY updated_at DESC LIMIT ?",
                    tuple(params),
                ).fetchall()
        return {"favorites": [self._row_to_dict(r) for r in rows], "engine": "sqlite"}

    def rebuild_meili_index(self) -> Tuple[bool, Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM kb_favorites ORDER BY updated_at DESC").fetchall()
        docs = [self._to_meili_doc(self._row_to_dict(r)) for r in rows]
        ok = self.meili.rebuild_documents(docs)
        return ok, {"rebuilt_count": len(docs), "meili_synced": ok}

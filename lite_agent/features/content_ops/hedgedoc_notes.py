"""
HedgeDoc v2 私有笔记同步与个人知识库检索服务 (features.content_ops.hedgedoc_notes).

归属 `features.content_ops`（与 `meili_sync_service.py` 同级），作为个人知识库/资产检索中枢
的可变资产同步层（后续 ebooks / music 等个人资产同步均平级归属此处，与 pc_cli 解耦）。

核心安全与隐私契约（Fail-Closed）：
1. 只读预检闸门：首次/每次同步前必须校验 HedgeDoc PostgreSQL `note.is_locked` 字段存在，
   未完成迁移或无法读取锁定状态时拒绝导入。
2. 三级隐私交付规则：
   - 活跃且无排除规则：本人可搜索，自动引用/热点写作可召回；
   - 标记 `不引用` 或 `kb_note_rules.exclude_from_quote=1`：本人可搜索并显示出处，
     在取正文/组装写作提示词前强制过滤；
   - `is_locked=True`、标记 `不索引`、`kb_note_rules.exclude_from_index=1` 或已删除：
     不返回标题/摘要/正文，不进入任何模型上下文。
3. 交付前实时复核：Meilisearch 只提供候选集，返回给前端或写作模型前必须回源复核当前
   `is_locked` / 标签 / `kb_note_rules` 状态；查不到状态即丢弃候选，已排除条目立即提交
   Meilisearch 删除并等待任务确认。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger("features.content_ops.hedgedoc_notes")

DEFAULT_ARCHIVE_DB_PATH = "data/output_archive.db"
NOTES_MEILI_INDEX = "notes"
DEFAULT_HEDGEDOC_PUBLIC_URL = "https://md.maifeipin.com"

NO_INDEX_TAGS = frozenset({"不索引", "noindex", "绝密"})
NO_QUOTE_TAGS = frozenset({"不引用", "noquote"})

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,3})\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_kb_owners(config: Optional[Dict[str, Any]] = None) -> Set[str]:
    """解析个人知识库所有者用户名白名单（与登录端点账户源对齐，多账户默认 fail-closed）。"""
    cfg = config if isinstance(config, dict) else {}
    kb_cfg = cfg.get("personal_kb", {}) if isinstance(cfg.get("personal_kb"), dict) else {}
    hd_cfg = cfg.get("hedgedoc_notes", {}) if isinstance(cfg.get("hedgedoc_notes"), dict) else {}
    pc_cfg = cfg.get("pc_cli", {}) if isinstance(cfg.get("pc_cli"), dict) else {}
    owners: Set[str] = set()

    for raw_list in (
        kb_cfg.get("owners"),
        hd_cfg.get("owners"),
        pc_cfg.get("favorites_owners"),
        pc_cfg.get("owner_usernames"),
        cfg.get("owner_usernames"),
    ):
        if isinstance(raw_list, list):
            owners.update(str(u).strip() for u in raw_list if str(u).strip())
        elif isinstance(raw_list, str):
            owners.update(u.strip() for u in raw_list.replace("，", ",").split(",") if u.strip())

    for single in (
        kb_cfg.get("owner_username"),
        hd_cfg.get("owner_username"),
        pc_cfg.get("owner_username"),
        cfg.get("owner_username"),
        os.environ.get("LITE_AGENT_OWNER_USERNAME"),
    ):
        if single and str(single).strip():
            owners.add(str(single).strip())

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


def extract_markdown_headings(content: str, max_headings: int = 30) -> List[str]:
    """提取 Markdown 文档中的 H1–H3 标题文本（去重保序，忽略围栏代码块）。"""
    if not content:
        return []
    headings: List[str] = []
    seen = set()
    in_fence = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        text = m.group(2).strip()
        if text and text not in seen:
            seen.add(text)
            headings.append(text[:120])
            if len(headings) >= max_headings:
                break
    return headings


def evaluate_note_privacy(
    source_status: Optional[Dict[str, Any]],
    rule: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """根据源库实时状态与本地 kb_note_rules 计算最终隐私策略（Fail-Closed）。"""
    if not source_status or not source_status.get("exists", True):
        return {
            "exists": False,
            "is_locked": False,
            "exclude_from_index": True,
            "exclude_from_quote": True,
            "reason": "missing_or_deleted",
        }

    is_locked = bool(source_status.get("is_locked", False))
    raw_tags = source_status.get("tags") or []
    tags_lower = {str(t).strip().lower() for t in raw_tags if str(t).strip()}
    rule_no_index = bool((rule or {}).get("exclude_from_index", False))
    rule_no_quote = bool((rule or {}).get("exclude_from_quote", False))

    tag_no_index = bool(tags_lower & NO_INDEX_TAGS)
    tag_no_quote = bool(tags_lower & NO_QUOTE_TAGS)

    exclude_from_index = is_locked or tag_no_index or rule_no_index
    exclude_from_quote = exclude_from_index or tag_no_quote or rule_no_quote

    reason = ""
    if is_locked:
        reason = "locked"
    elif tag_no_index:
        reason = "tag_no_index"
    elif rule_no_index:
        reason = "rule_no_index"
    elif tag_no_quote:
        reason = "tag_no_quote"
    elif rule_no_quote:
        reason = "rule_no_quote"

    return {
        "exists": True,
        "is_locked": is_locked,
        "exclude_from_index": exclude_from_index,
        "exclude_from_quote": exclude_from_quote,
        "reason": reason,
    }


def compute_projection_hash(doc: Dict[str, Any]) -> str:
    """计算索引文档全投影哈希，覆盖标题、目录、标签、标题大纲、正文、链接与引用开关。"""
    payload = {
        "alias": str(doc.get("alias") or ""),
        "url": str(doc.get("url") or ""),
        "title": str(doc.get("title") or ""),
        "folder_path": str(doc.get("folder_path") or ""),
        "folder_id": doc.get("folder_id"),
        "tags": sorted(str(t) for t in (doc.get("tags") or [])),
        "headings": list(doc.get("headings") or []),
        "content": str(doc.get("content") or ""),
        "exclude_from_quote": bool(doc.get("exclude_from_quote", False)),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class HedgeDocSource(Protocol):
    """HedgeDoc 只读数据源抽象协议，便于单测注入与生产直连解耦。"""

    def preflight_check(self) -> Tuple[bool, str]:
        ...

    def fetch_active_notes(
        self,
        note_ids: Optional[Sequence[int]] = None,
    ) -> List[Dict[str, Any]]:
        ...

    def fetch_live_statuses(self, note_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
        ...


class PostgresHedgeDocSource:
    """基于 psycopg2 的 HedgeDoc v2 只读数据源（强制只读事务）。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config if isinstance(config, dict) else {}
        hd_cfg = cfg.get("hedgedoc_notes", {}) if isinstance(cfg.get("hedgedoc_notes"), dict) else {}
        if not hd_cfg:
            try:
                from lite_agent.core.config_loader import load_config

                full_cfg = load_config()
                if isinstance(full_cfg, dict) and isinstance(full_cfg.get("hedgedoc_notes"), dict):
                    hd_cfg = full_cfg["hedgedoc_notes"]
            except Exception:
                hd_cfg = {}

        self.dsn = str(hd_cfg.get("dsn") or os.environ.get("HEDGEDOC_DB_DSN") or "").strip()
        self.host = str(hd_cfg.get("host") or os.environ.get("HEDGEDOC_DB_HOST") or "").strip()
        self.port = int(hd_cfg.get("port") or os.environ.get("HEDGEDOC_DB_PORT") or 5432)
        self.dbname = str(hd_cfg.get("database") or os.environ.get("HEDGEDOC_DB_NAME") or "hedgedoc").strip()
        self.user = str(hd_cfg.get("user") or os.environ.get("HEDGEDOC_DB_USER") or "hedgedoc").strip()
        self.password = str(hd_cfg.get("password") or os.environ.get("HEDGEDOC_DB_PASSWORD") or "").strip()
        self.connect_timeout = int(hd_cfg.get("connect_timeout") or 5)

    def is_configured(self) -> bool:
        return bool(self.dsn or self.host)

    def _connect(self):
        import psycopg2

        if not self.is_configured():
            raise RuntimeError("HedgeDoc PostgreSQL 未配置 (hedgedoc_notes.host 或 dsn)")
        if self.dsn:
            conn = psycopg2.connect(
                self.dsn,
                connect_timeout=self.connect_timeout,
                options="-c default_transaction_read_only=on",
            )
        else:
            conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                dbname=self.dbname,
                user=self.user,
                password=self.password,
                connect_timeout=self.connect_timeout,
                options="-c default_transaction_read_only=on",
            )
        conn.set_session(readonly=True, autocommit=True)
        return conn

    def preflight_check(self) -> Tuple[bool, str]:
        if not self.is_configured():
            return False, "HedgeDoc 数据库未配置 (hedgedoc_notes.host / dsn)"
        conn = None
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'note'
                      AND column_name = 'is_locked'
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
            if not row:
                return False, "HedgeDoc 数据库尚未完成 note.is_locked 迁移，拒绝导入"
            return True, "ok"
        except Exception as e:
            return False, f"HedgeDoc 预检连接失败: {e}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def fetch_active_notes(
        self,
        note_ids: Optional[Sequence[int]] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT id, name, "parentId" FROM folders')
                folder_rows = cur.fetchall()
                folder_map = {int(r[0]): (str(r[1] or ""), int(r[2]) if r[2] is not None else None) for r in folder_rows}

                def build_folder_path(fid: Optional[int]) -> str:
                    if fid is None:
                        return ""
                    parts: List[str] = []
                    visited = set()
                    curr: Optional[int] = fid
                    while curr is not None and curr not in visited and curr in folder_map:
                        visited.add(curr)
                        name, parent_id = folder_map[curr]
                        if name:
                            parts.append(name)
                        curr = parent_id
                    parts.reverse()
                    return "/".join(parts)

                where_clause = ""
                params: Tuple[Any, ...] = ()
                if note_ids is not None:
                    id_list = [int(nid) for nid in note_ids]
                    if not id_list:
                        return []
                    where_clause = "WHERE n.id = ANY(%s)"
                    params = (id_list,)

                sql = f"""
                    WITH latest_rev AS (
                        SELECT DISTINCT ON (note_id)
                            note_id, title, content, created_at AS updated_at
                        FROM revision
                        ORDER BY note_id, created_at DESC
                    ),
                    primary_alias AS (
                        SELECT DISTINCT ON (note_id)
                            note_id, alias
                        FROM alias
                        ORDER BY note_id, is_primary DESC, alias ASC
                    ),
                    note_tag_agg AS (
                        SELECT nt."noteId" AS note_id, array_agg(t.name ORDER BY t.name) AS tags
                        FROM note_tags nt
                        INNER JOIN tags t ON t.id = nt."tagId"
                        GROUP BY nt."noteId"
                    )
                    SELECT
                        n.id,
                        n.is_locked,
                        n.folder_id,
                        n.created_at,
                        COALESCE(pa.alias, '') AS alias,
                        COALESCE(lr.title, '') AS title,
                        COALESCE(lr.content, '') AS content,
                        COALESCE(lr.updated_at, n.created_at) AS updated_at,
                        COALESCE(nta.tags, ARRAY[]::text[]) AS tags
                    FROM note n
                    INNER JOIN latest_rev lr ON lr.note_id = n.id
                    LEFT JOIN primary_alias pa ON pa.note_id = n.id
                    LEFT JOIN note_tag_agg nta ON nta.note_id = n.id
                    {where_clause}
                    ORDER BY n.id ASC
                """
                cur.execute(sql, params)
                rows = cur.fetchall()

            results: List[Dict[str, Any]] = []
            for r in rows:
                nid = int(r[0])
                fid = int(r[2]) if r[2] is not None else None
                created_at = r[3].isoformat() if hasattr(r[3], "isoformat") else str(r[3] or "")
                updated_at = r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7] or created_at)
                tags = [str(t).strip() for t in (r[8] or []) if str(t).strip()]
                results.append(
                    {
                        "note_id": nid,
                        "is_locked": bool(r[1]),
                        "folder_id": fid,
                        "folder_path": build_folder_path(fid),
                        "created_at": created_at,
                        "alias": str(r[4] or ""),
                        "title": str(r[5] or ""),
                        "content": str(r[6] or ""),
                        "updated_at": updated_at,
                        "tags": tags,
                    }
                )
            return results
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def fetch_live_statuses(self, note_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
        id_list = [int(nid) for nid in note_ids]
        if not id_list:
            return {}
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        n.id,
                        n.is_locked,
                        COALESCE(
                            (
                                SELECT array_agg(t.name ORDER BY t.name)
                                FROM note_tags nt
                                INNER JOIN tags t ON t.id = nt."tagId"
                                WHERE nt."noteId" = n.id
                            ),
                            ARRAY[]::text[]
                        ) AS tags
                    FROM note n
                    WHERE n.id = ANY(%s)
                    """,
                    (id_list,),
                )
                rows = cur.fetchall()
            out: Dict[int, Dict[str, Any]] = {}
            for r in rows:
                out[int(r[0])] = {
                    "exists": True,
                    "is_locked": bool(r[1]),
                    "tags": [str(t).strip() for t in (r[2] or []) if str(t).strip()],
                }
            return out
        finally:
            try:
                conn.close()
            except Exception:
                pass


class NotesMeiliClient:
    """独立私有 Meilisearch `notes` 索引客户端（同步确认任务完成）。"""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        index_name: str = NOTES_MEILI_INDEX,
        timeout: float = 5.0,
        task_wait_seconds: float = 10.0,
    ) -> None:
        cfg = config if isinstance(config, dict) else {}
        meili_cfg = cfg.get("meilisearch", {}) if isinstance(cfg.get("meilisearch"), dict) else {}
        if not meili_cfg.get("url"):
            try:
                from lite_agent.core.config_loader import load_config

                full_cfg = load_config()
                if isinstance(full_cfg, dict) and isinstance(full_cfg.get("meilisearch"), dict):
                    meili_cfg = full_cfg["meilisearch"]
            except Exception:
                pass
        self.base_url = str(meili_cfg.get("url") or os.environ.get("MEILISEARCH_URL") or "").rstrip("/")
        self.master_key = str(meili_cfg.get("master_key") or os.environ.get("MEILISEARCH_MASTER_KEY") or "").strip()
        self.index_name = index_name
        self.timeout = timeout
        self.task_wait_seconds = task_wait_seconds
        self._index_ready = False

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.master_key:
            headers["Authorization"] = f"Bearer {self.master_key}"
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Any] = None,
    ) -> Tuple[bool, Any]:
        if not self.is_configured():
            return False, "Meilisearch 未配置"
        url = f"{self.base_url}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urlrequest.Request(url, data=data, headers=self._headers(), method=method.upper())
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return True, json.loads(raw) if raw else {}
        except urlerror.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = str(e)
            return False, f"HTTP {e.code}: {err_body[:300]}"
        except Exception as e:
            return False, str(e)

    def wait_for_task(self, task_uid: Any) -> Tuple[bool, str]:
        if task_uid is None:
            return False, "missing_task_uid"
        deadline = time.monotonic() + max(0.2, self.task_wait_seconds)
        while time.monotonic() < deadline:
            ok, data = self._request_json("GET", f"/tasks/{task_uid}")
            if not ok or not isinstance(data, dict):
                return False, f"task_poll_error:{data}"
            status = str(data.get("status") or "")
            if status == "succeeded":
                return True, "succeeded"
            if status in ("failed", "canceled"):
                err = data.get("error") or {}
                return False, f"task_{status}:{err.get('message') if isinstance(err, dict) else err}"
            time.sleep(0.08)
        return False, "task_timeout"

    def ensure_index(self) -> bool:
        if self._index_ready:
            return True
        if not self.is_configured():
            return False
        ok, data = self._request_json("POST", "/indexes", {"uid": self.index_name, "primaryKey": "id"})
        if ok and isinstance(data, dict) and data.get("taskUid") is not None:
            self.wait_for_task(data.get("taskUid"))
        ok_s, data_s = self._request_json(
            "PATCH",
            f"/indexes/{self.index_name}/settings",
            {
                "searchableAttributes": ["title", "folder_path", "tags", "headings", "content"],
                "filterableAttributes": ["note_id", "folder_path", "tags", "exclude_from_quote"],
                "sortableAttributes": ["updated_at", "created_at"],
            },
        )
        if ok_s and isinstance(data_s, dict) and data_s.get("taskUid") is not None:
            self.wait_for_task(data_s.get("taskUid"))
        self._index_ready = True
        return True

    def upsert_documents(self, docs: List[Dict[str, Any]]) -> Tuple[bool, str]:
        if not docs:
            return True, "noop"
        if not self.is_configured():
            return False, "Meilisearch 未配置"
        self.ensure_index()
        ok, res = self._request_json("POST", f"/indexes/{self.index_name}/documents?primaryKey=id", docs)
        if not ok:
            return False, str(res)
        task_uid = res.get("taskUid") if isinstance(res, dict) else None
        return self.wait_for_task(task_uid)

    def delete_documents(self, doc_ids: List[str]) -> Tuple[bool, str]:
        if not doc_ids:
            return True, "noop"
        if not self.is_configured():
            return False, "Meilisearch 未配置"
        self.ensure_index()
        ok, res = self._request_json("POST", f"/indexes/{self.index_name}/documents/delete-batch", doc_ids)
        if not ok:
            return False, str(res)
        task_uid = res.get("taskUid") if isinstance(res, dict) else None
        return self.wait_for_task(task_uid)

    def list_document_ids(self, page_size: int = 1000) -> Tuple[bool, List[int], str]:
        """分页列出 Meilisearch 索引中实际存在的文档 ID，用于全量对账兜底清理迟到写入。"""
        if not self.is_configured():
            return False, [], "Meilisearch 未配置"
        ids: List[int] = []
        offset = 0
        limit = max(1, min(int(page_size), 1000))
        while True:
            ok, res = self._request_json(
                "GET",
                f"/indexes/{self.index_name}/documents?fields=id&limit={limit}&offset={offset}",
            )
            if not ok:
                # 索引尚未创建时返回空列表而非报错
                if "index_not_found" in str(res) or "HTTP 404" in str(res):
                    return True, [], ""
                return False, [], str(res)
            results = res.get("results") if isinstance(res, dict) else []
            if not isinstance(results, list) or not results:
                break
            for row in results:
                if isinstance(row, dict) and row.get("id") is not None:
                    try:
                        ids.append(int(row["id"]))
                    except Exception:
                        continue
            if len(results) < limit:
                break
            offset += limit
        return True, ids, ""

    def search(
        self,
        query: str,
        limit: int = 30,
        folder_path: Optional[str] = None,
    ) -> Tuple[bool, List[Dict[str, Any]], str]:
        if not self.is_configured():
            return False, [], "Meilisearch 未配置"
        payload: Dict[str, Any] = {
            "q": query,
            "limit": max(1, min(int(limit), 100)),
            "attributesToCrop": ["content"],
            "cropLength": 160,
        }
        if folder_path:
            safe_fp = str(folder_path).replace('"', '\\"')
            payload["filter"] = f'folder_path = "{safe_fp}"'
        ok, res = self._request_json("POST", f"/indexes/{self.index_name}/search", payload)
        if not ok or not isinstance(res, dict):
            return False, [], str(res)
        hits = res.get("hits") if isinstance(res.get("hits"), list) else []
        return True, hits, ""


class HedgeDocNotesService:
    """HedgeDoc 私有笔记同步、手动排除规则与交付前实时复核检索服务。"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        source: Optional[HedgeDocSource] = None,
        meili: Optional[NotesMeiliClient] = None,
        public_base_url: Optional[str] = None,
    ) -> None:
        cfg = config if isinstance(config, dict) else {}
        hd_cfg = cfg.get("hedgedoc_notes", {}) if isinstance(cfg.get("hedgedoc_notes"), dict) else {}
        pc_cfg = cfg.get("pc_cli", {}) if isinstance(cfg.get("pc_cli"), dict) else {}
        self.db_path = str(db_path or hd_cfg.get("archive_db_path") or pc_cfg.get("archive_db_path") or DEFAULT_ARCHIVE_DB_PATH)
        self.public_base_url = str(
            public_base_url
            or hd_cfg.get("public_base_url")
            or os.environ.get("HEDGEDOC_PUBLIC_URL")
            or DEFAULT_HEDGEDOC_PUBLIC_URL
        ).rstrip("/")
        self.allowed_owners: Set[str] = resolve_kb_owners(cfg)
        self.source: HedgeDocSource = source if source is not None else PostgresHedgeDocSource(config=cfg)
        self.meili: NotesMeiliClient = meili if meili is not None else NotesMeiliClient(config=cfg)
        self._schema_ready = False
        self._sync_lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        if not self._schema_ready:
            self._ensure_schema(conn)
            self._schema_ready = True
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kb_note_rules (
                note_id INTEGER PRIMARY KEY,
                exclude_from_index INTEGER NOT NULL DEFAULT 0,
                exclude_from_quote INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb_notes_sync_state (
                note_id INTEGER PRIMARY KEY,
                projection_hash TEXT NOT NULL,
                alias TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                folder_path TEXT NOT NULL DEFAULT '',
                exclude_from_quote INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            """
        )

    def preflight(self) -> Dict[str, Any]:
        ok, detail = self.source.preflight_check()
        return {
            "preflight_ok": ok,
            "detail": detail,
            "meili_configured": self.meili.is_configured(),
        }

    def get_note_rules(self, note_ids: Optional[Sequence[int]] = None) -> Dict[int, Dict[str, Any]]:
        with self._connect() as conn:
            if note_ids is not None:
                id_list = [int(nid) for nid in note_ids]
                if not id_list:
                    return {}
                placeholders = ",".join("?" for _ in id_list)
                rows = conn.execute(
                    f"SELECT note_id, exclude_from_index, exclude_from_quote, updated_at FROM kb_note_rules WHERE note_id IN ({placeholders})",
                    id_list,
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT note_id, exclude_from_index, exclude_from_quote, updated_at FROM kb_note_rules"
                ).fetchall()
        return {
            int(r["note_id"]): {
                "note_id": int(r["note_id"]),
                "exclude_from_index": bool(r["exclude_from_index"]),
                "exclude_from_quote": bool(r["exclude_from_quote"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        }

    def list_rules(self) -> List[Dict[str, Any]]:
        rules = self.get_note_rules()
        return [rules[k] for k in sorted(rules.keys())]

    def _get_sync_state(self, note_ids: Optional[Sequence[int]] = None) -> Dict[int, str]:
        with self._connect() as conn:
            if note_ids is not None:
                id_list = [int(nid) for nid in note_ids]
                if not id_list:
                    return {}
                placeholders = ",".join("?" for _ in id_list)
                rows = conn.execute(
                    f"SELECT note_id, projection_hash FROM kb_notes_sync_state WHERE note_id IN ({placeholders})",
                    id_list,
                ).fetchall()
            else:
                rows = conn.execute("SELECT note_id, projection_hash FROM kb_notes_sync_state").fetchall()
        return {int(r["note_id"]): str(r["projection_hash"]) for r in rows}

    def _record_synced_docs(self, docs: List[Dict[str, Any]], hashes: Dict[int, str]) -> None:
        if not docs:
            return
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO kb_notes_sync_state
                    (note_id, projection_hash, alias, title, folder_path, exclude_from_quote, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(note_id) DO UPDATE SET
                    projection_hash = excluded.projection_hash,
                    alias = excluded.alias,
                    title = excluded.title,
                    folder_path = excluded.folder_path,
                    exclude_from_quote = excluded.exclude_from_quote,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        int(d["note_id"]),
                        hashes[int(d["note_id"])],
                        str(d.get("alias") or ""),
                        str(d.get("title") or ""),
                        str(d.get("folder_path") or ""),
                        1 if d.get("exclude_from_quote") else 0,
                        now,
                    )
                    for d in docs
                ],
            )

    def _remove_sync_state(self, note_ids: Sequence[int]) -> None:
        id_list = [int(nid) for nid in note_ids]
        if not id_list:
            return
        placeholders = ",".join("?" for _ in id_list)
        with self._connect() as conn:
            conn.execute(f"DELETE FROM kb_notes_sync_state WHERE note_id IN ({placeholders})", id_list)

    def _build_meili_doc(self, raw: Dict[str, Any], exclude_from_quote: bool) -> Dict[str, Any]:
        nid = int(raw["note_id"])
        alias = str(raw.get("alias") or "").strip()
        url = f"{self.public_base_url}/{alias}" if alias else ""
        content = str(raw.get("content") or "")
        title = str(raw.get("title") or "").strip() or (alias or f"Note #{nid}")
        tags = sorted({str(t).strip().lower() for t in (raw.get("tags") or []) if str(t).strip()})
        return {
            "id": str(nid),
            "note_id": nid,
            "alias": alias,
            "url": url,
            "title": title,
            "folder_id": raw.get("folder_id"),
            "folder_path": str(raw.get("folder_path") or ""),
            "tags": tags,
            "headings": extract_markdown_headings(content),
            "content": content,
            "exclude_from_quote": bool(exclude_from_quote),
            "created_at": str(raw.get("created_at") or ""),
            "updated_at": str(raw.get("updated_at") or ""),
        }

    def sync_notes(
        self,
        note_ids: Optional[Sequence[int]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        执行全量或指定 ID 集合的对账同步（与 set_note_rules 串行化，防止旧快照回写刚排除笔记）：
        - 先执行只读预检（缺失 note.is_locked 立即拒绝）；
        - 全量对账同时扫描 Meilisearch 实际存在的 ID，清理迟到成功或已排除的孤儿文档；
        - 提交写入前再次复核实时源库与 kb_note_rules 状态，防止并发排除被回写；
        - 仅在 Meilisearch 任务确认 succeeded 后推进 kb_notes_sync_state。
        """
        with self._sync_lock:
            ok_pre, pre_detail = self.source.preflight_check()
            if not ok_pre:
                return False, {"preflight_ok": False, "error": pre_detail}

            if not self.meili.is_configured():
                return False, {"preflight_ok": True, "error": "Meilisearch 未配置"}

            scoped_ids = [int(nid) for nid in note_ids] if note_ids is not None else None
            active_notes = self.source.fetch_active_notes(note_ids=scoped_ids)
            rules = self.get_note_rules(note_ids=scoped_ids)
            prev_hashes = self._get_sync_state(note_ids=scoped_ids)

            meili_existing_ids: Set[int] = set()
            if scoped_ids is None and hasattr(self.meili, "list_document_ids"):
                ok_ids, actual_ids, _err_ids = self.meili.list_document_ids()
                if ok_ids:
                    meili_existing_ids = set(actual_ids)

            seen_ids: Set[int] = set()
            to_upsert: List[Dict[str, Any]] = []
            to_upsert_hashes: Dict[int, str] = {}
            to_delete: List[int] = []
            skipped_unchanged = 0
            excluded_count = 0

            for raw in active_notes:
                nid = int(raw["note_id"])
                seen_ids.add(nid)
                status = {
                    "exists": True,
                    "is_locked": bool(raw.get("is_locked", False)),
                    "tags": raw.get("tags") or [],
                }
                privacy = evaluate_note_privacy(status, rules.get(nid))
                if privacy["exclude_from_index"]:
                    excluded_count += 1
                    if nid in prev_hashes or nid in meili_existing_ids or scoped_ids is not None:
                        to_delete.append(nid)
                    continue

                doc = self._build_meili_doc(raw, exclude_from_quote=privacy["exclude_from_quote"])
                proj_hash = compute_projection_hash(doc)
                if prev_hashes.get(nid) == proj_hash and (scoped_ids is not None or not meili_existing_ids or nid in meili_existing_ids):
                    skipped_unchanged += 1
                    continue
                to_upsert.append(doc)
                to_upsert_hashes[nid] = proj_hash

            if scoped_ids is None:
                for old_nid in set(prev_hashes.keys()) | meili_existing_ids:
                    if old_nid not in seen_ids:
                        to_delete.append(old_nid)
            else:
                for req_nid in scoped_ids:
                    if req_nid not in seen_ids and req_nid in prev_hashes:
                        to_delete.append(req_nid)

            # 提交写入前二次复核：若在抓取与提交之间笔记被上锁/标不索引/改规则，绝不写回索引
            if to_upsert:
                up_ids = [int(d["note_id"]) for d in to_upsert]
                live_recheck = self.source.fetch_live_statuses(up_ids)
                rules_recheck = self.get_note_rules(up_ids)
                verified_upsert: List[Dict[str, Any]] = []
                verified_hashes: Dict[int, str] = {}
                for doc in to_upsert:
                    nid = int(doc["note_id"])
                    privacy_now = evaluate_note_privacy(live_recheck.get(nid), rules_recheck.get(nid))
                    if privacy_now["exclude_from_index"]:
                        excluded_count += 1
                        to_delete.append(nid)
                        continue
                    if bool(doc.get("exclude_from_quote")) != bool(privacy_now["exclude_from_quote"]):
                        doc["exclude_from_quote"] = bool(privacy_now["exclude_from_quote"])
                        to_upsert_hashes[nid] = compute_projection_hash(doc)
                    verified_upsert.append(doc)
                    verified_hashes[nid] = to_upsert_hashes[nid]
                to_upsert = verified_upsert
                to_upsert_hashes = verified_hashes

            to_delete = sorted(set(to_delete))

            if to_delete:
                ok_del, del_detail = self.meili.delete_documents([str(nid) for nid in to_delete])
                if not ok_del:
                    return False, {
                        "preflight_ok": True,
                        "error": f"Meilisearch 删除确认失败: {del_detail}",
                        "pending_delete": len(to_delete),
                    }
                self._remove_sync_state(to_delete)

            if to_upsert:
                ok_up, up_detail = self.meili.upsert_documents(to_upsert)
                if not ok_up:
                    return False, {
                        "preflight_ok": True,
                        "error": f"Meilisearch 写入确认失败: {up_detail}",
                        "deleted": len(to_delete),
                        "pending_upsert": len(to_upsert),
                    }
                self._record_synced_docs(to_upsert, to_upsert_hashes)

            return True, {
                "preflight_ok": True,
                "scanned": len(active_notes),
                "upserted": len(to_upsert),
                "deleted": len(to_delete),
                "skipped_unchanged": skipped_unchanged,
                "excluded_count": excluded_count,
            }

    def set_note_rules(
        self,
        note_id: int,
        exclude_from_index: Optional[bool] = None,
        exclude_from_quote: Optional[bool] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """更新单篇笔记的手动排除规则，并与同步写入串行化立即对账 Meilisearch 索引。"""
        with self._sync_lock:
            nid = int(note_id)
            existing = self.get_note_rules([nid]).get(nid) or {
                "exclude_from_index": False,
                "exclude_from_quote": False,
            }
            next_no_index = bool(existing["exclude_from_index"]) if exclude_from_index is None else bool(exclude_from_index)
            next_no_quote = bool(existing["exclude_from_quote"]) if exclude_from_quote is None else bool(exclude_from_quote)
            now = _utc_now_iso()

            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO kb_note_rules (note_id, exclude_from_index, exclude_from_quote, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(note_id) DO UPDATE SET
                        exclude_from_index = excluded.exclude_from_index,
                        exclude_from_quote = excluded.exclude_from_quote,
                        updated_at = excluded.updated_at
                    """,
                    (nid, 1 if next_no_index else 0, 1 if next_no_quote else 0, now),
                )

            rule_obj = {
                "note_id": nid,
                "exclude_from_index": next_no_index,
                "exclude_from_quote": next_no_quote,
                "updated_at": now,
            }

            if next_no_index:
                sync_info: Dict[str, Any] = {"action": "delete"}
                if self.meili.is_configured():
                    ok_del, detail = self.meili.delete_documents([str(nid)])
                    sync_info["ok"] = ok_del
                    sync_info["detail"] = detail
                    if ok_del:
                        self._remove_sync_state([nid])
                else:
                    sync_info["ok"] = True
                    sync_info["detail"] = "meili_not_configured"
                    self._remove_sync_state([nid])
                return True, {"rule": rule_obj, "sync": sync_info}

            ok_sync, sync_res = self.sync_notes(note_ids=[nid])
            return True, {"rule": rule_obj, "sync": {"ok": ok_sync, **sync_res}}

    def get_note_for_quote(self, note_id: int) -> Tuple[bool, Dict[str, Any]]:
        """
        引用前实时回源复核（供一键引用与发送回合前服务端校验，Fail-Closed）：
        - 若已锁定/打不索引/设置 exclude_from_index/已删：拒绝并立即从 Meilisearch 清除；
        - 若打不引用/设置 exclude_from_quote：拒绝引用，不返回正文；
        - 仅在完全通过复核时返回最新标题、出处与正文。
        """
        nid = int(note_id)
        try:
            rows = self.source.fetch_active_notes(note_ids=[nid])
        except Exception as e:
            return False, {"code": "SOURCE_UNAVAILABLE", "error": f"源库实时复核失败，禁止引用: {e}"}

        if not rows:
            if self.meili.is_configured():
                ok_del, _ = self.meili.delete_documents([str(nid)])
                if ok_del:
                    self._remove_sync_state([nid])
            return False, {"code": "NOTE_NOT_FOUND", "error": "笔记不存在或已删除"}

        raw = rows[0]
        live_status = {
            "exists": True,
            "is_locked": bool(raw.get("is_locked", False)),
            "tags": raw.get("tags") or [],
        }
        rule = self.get_note_rules([nid]).get(nid)
        privacy = evaluate_note_privacy(live_status, rule)

        if privacy["exclude_from_index"]:
            if self.meili.is_configured():
                ok_del, _ = self.meili.delete_documents([str(nid)])
                if ok_del:
                    self._remove_sync_state([nid])
            return False, {
                "code": "NOTE_EXCLUDED_FROM_INDEX",
                "error": "该笔记已锁定或已排除索引，禁止引用",
            }

        if privacy["exclude_from_quote"]:
            return False, {
                "code": "NOTE_EXCLUDED_FROM_QUOTE",
                "error": "该笔记已标记排除引用，禁止进入模型上下文",
            }

        doc = self._build_meili_doc(raw, exclude_from_quote=False)
        return True, {
            "note": {
                "note_id": nid,
                "alias": doc["alias"],
                "url": doc["url"],
                "title": doc["title"],
                "folder_path": doc["folder_path"],
                "tags": doc["tags"],
                "snippet": str(doc["content"])[:320],
                "content": doc["content"],
                "updated_at": doc["updated_at"],
            }
        }

    def search_notes(
        self,
        query: str,
        limit: int = 20,
        purpose: str = "search",
        folder_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        检索私有笔记（交付前强复核源库实时状态，Fail-Closed）：
        - purpose="search": 本人手动检索（保留 `不引用` 条目并带标记，丢弃上锁/`不索引`/已删条目）
        - purpose="quote": 热点写作与自动引用（额外过滤 `不引用` 与 `exclude_from_quote`）
        """
        norm_purpose = "quote" if str(purpose or "").strip().lower() == "quote" else "search"
        safe_limit = max(1, min(int(limit or 20), 50))
        fetch_limit = max(safe_limit * 3, 30)

        ok_search, hits, err = self.meili.search(query=query, limit=fetch_limit, folder_path=folder_path)
        if not ok_search:
            return {
                "items": [],
                "total": 0,
                "purpose": norm_purpose,
                "error": err,
                "purged_stale": 0,
            }

        if not hits:
            return {
                "items": [],
                "total": 0,
                "purpose": norm_purpose,
                "purged_stale": 0,
            }

        cand_ids: List[int] = []
        for h in hits:
            raw_id = h.get("note_id", h.get("id"))
            try:
                cand_ids.append(int(raw_id))
            except Exception:
                continue

        try:
            live_statuses = self.source.fetch_live_statuses(cand_ids)
        except Exception as e:
            logger.warning(f"HedgeDoc 交付前实时复核失败 (Fail-Closed): {e}")
            return {
                "items": [],
                "total": 0,
                "purpose": norm_purpose,
                "error": f"源库实时状态复核失败，已拒绝返回候选: {e}",
                "purged_stale": 0,
            }

        rules = self.get_note_rules(cand_ids)
        verified_items: List[Dict[str, Any]] = []
        stale_delete_ids: List[int] = []

        for h in hits:
            raw_id = h.get("note_id", h.get("id"))
            try:
                nid = int(raw_id)
            except Exception:
                continue

            live = live_statuses.get(nid)
            privacy = evaluate_note_privacy(live, rules.get(nid))

            if privacy["exclude_from_index"]:
                stale_delete_ids.append(nid)
                continue

            if norm_purpose == "quote" and privacy["exclude_from_quote"]:
                continue

            formatted = h.get("_formatted") if isinstance(h.get("_formatted"), dict) else {}
            snippet = str(formatted.get("content") or h.get("content") or "")[:320]
            item: Dict[str, Any] = {
                "note_id": nid,
                "alias": str(h.get("alias") or ""),
                "url": str(h.get("url") or ""),
                "title": str(h.get("title") or ""),
                "folder_path": str(h.get("folder_path") or ""),
                "tags": list(live.get("tags") or h.get("tags") or []) if live else list(h.get("tags") or []),
                "headings": list(h.get("headings") or []),
                "snippet": snippet,
                "exclude_from_quote": bool(privacy["exclude_from_quote"]),
                "updated_at": str(h.get("updated_at") or ""),
            }
            if norm_purpose == "quote":
                item["content"] = str(h.get("content") or "")
            verified_items.append(item)
            if len(verified_items) >= safe_limit:
                break

        purged_stale = 0
        if stale_delete_ids:
            unique_stale = sorted(set(stale_delete_ids))
            ok_del, del_detail = self.meili.delete_documents([str(nid) for nid in unique_stale])
            if ok_del:
                self._remove_sync_state(unique_stale)
                purged_stale = len(unique_stale)
            else:
                logger.warning(f"清理失效笔记索引失败 ({unique_stale}): {del_detail}")

        return {
            "items": verified_items,
            "total": len(verified_items),
            "purpose": norm_purpose,
            "purged_stale": purged_stale,
        }

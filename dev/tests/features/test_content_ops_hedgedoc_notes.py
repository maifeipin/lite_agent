"""HedgeDoc v2 私有笔记同步（features.content_ops）、三级隐私规则与交付前实时复核单元测试。"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from lite_agent.features.content_ops import kb_routes
from lite_agent.features.content_ops.hedgedoc_notes import (
    HedgeDocNotesService,
    extract_markdown_headings,
)


class FakeHedgeDocSource:
    def __init__(self, preflight_ok: bool = True, preflight_msg: str = "ok") -> None:
        self.preflight_ok = preflight_ok
        self.preflight_msg = preflight_msg
        self.notes: Dict[int, Dict[str, Any]] = {}
        self.fail_live_check = False

    def preflight_check(self) -> Tuple[bool, str]:
        return self.preflight_ok, self.preflight_msg

    def fetch_active_notes(
        self,
        note_ids: Optional[Sequence[int]] = None,
    ) -> List[Dict[str, Any]]:
        if note_ids is None:
            return [dict(self.notes[k]) for k in sorted(self.notes.keys())]
        return [dict(self.notes[int(k)]) for k in note_ids if int(k) in self.notes]

    def fetch_live_statuses(self, note_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
        if self.fail_live_check:
            raise RuntimeError("postgres unreachable")
        out: Dict[int, Dict[str, Any]] = {}
        for nid in note_ids:
            row = self.notes.get(int(nid))
            if row is not None:
                out[int(nid)] = {
                    "exists": True,
                    "is_locked": bool(row.get("is_locked", False)),
                    "tags": list(row.get("tags") or []),
                }
        return out


class FakeNotesMeili:
    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.upsert_calls: List[List[str]] = []
        self.delete_calls: List[List[str]] = []

    def is_configured(self) -> bool:
        return True

    def upsert_documents(self, docs: List[Dict[str, Any]]) -> Tuple[bool, str]:
        ids = []
        for d in docs:
            did = str(d["id"])
            self.docs[did] = dict(d)
            ids.append(did)
        self.upsert_calls.append(ids)
        return True, "succeeded"

    def delete_documents(self, doc_ids: List[str]) -> Tuple[bool, str]:
        for did in doc_ids:
            self.docs.pop(str(did), None)
        self.delete_calls.append(list(doc_ids))
        return True, "succeeded"

    def search(
        self,
        query: str,
        limit: int = 30,
        folder_path: Optional[str] = None,
    ) -> Tuple[bool, List[Dict[str, Any]], str]:
        q = (query or "").strip().lower()
        hits = []
        for d in self.docs.values():
            if folder_path and d.get("folder_path") != folder_path:
                continue
            hay = " ".join(
                [
                    str(d.get("title") or ""),
                    str(d.get("folder_path") or ""),
                    " ".join(d.get("tags") or []),
                    " ".join(d.get("headings") or []),
                    str(d.get("content") or ""),
                ]
            ).lower()
            if not q or q in hay:
                hits.append(dict(d))
        return True, hits[:limit], ""


class _DummyHandler:
    def __init__(
        self,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        *,
        is_guest: bool = False,
        is_edge: bool = False,
        user_session: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.command = method
        self.is_guest = is_guest
        self.is_edge = is_edge
        self.user_session = user_session
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status_code = 0

    def send_response(self, code: int) -> None:
        self.status_code = code

    def send_header(self, _k: str, _v: str) -> None:
        pass

    def end_headers(self) -> None:
        pass

    def json_body(self) -> Dict[str, Any]:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_preflight_blocks_sync_when_is_locked_missing(tmp_path):
    source = FakeHedgeDocSource(preflight_ok=False, preflight_msg="note.is_locked missing")
    source.notes[1] = {
        "note_id": 1,
        "is_locked": False,
        "folder_id": 10,
        "folder_path": "工作",
        "alias": "note-1",
        "title": "架构复盘",
        "content": "# 架构复盘\n正文",
        "tags": [],
        "created_at": "2026-11-01T00:00:00",
        "updated_at": "2026-11-28T00:00:00",
    }
    meili = FakeNotesMeili()
    svc = HedgeDocNotesService(db_path=str(tmp_path / "archive.db"), source=source, meili=meili)

    ok, res = svc.sync_notes()
    assert ok is False
    assert res["preflight_ok"] is False
    assert meili.docs == {}
    assert meili.upsert_calls == []


def test_three_tier_privacy_and_live_recheck_before_delivery(tmp_path):
    source = FakeHedgeDocSource()
    source.notes = {
        1: {
            "note_id": 1,
            "is_locked": False,
            "folder_id": 10,
            "folder_path": "工作/架构",
            "alias": "arch-1",
            "title": "普通工作笔记",
            "content": "# 架构设计\n## 缓存策略\nMeilisearch 选型记录",
            "tags": ["工作"],
            "created_at": "2026-11-01T00:00:00",
            "updated_at": "2026-11-28T00:00:00",
        },
        2: {
            "note_id": 2,
            "is_locked": False,
            "folder_id": 11,
            "folder_path": "个人资料",
            "alias": "personal-2",
            "title": "不引用个人复盘",
            "content": "Meilisearch 个人复盘，不要自动引用",
            "tags": ["不引用"],
            "created_at": "2026-11-01T00:00:00",
            "updated_at": "2026-11-28T00:00:00",
        },
        3: {
            "note_id": 3,
            "is_locked": True,
            "folder_id": 11,
            "folder_path": "个人资料",
            "alias": "locked-3",
            "title": "已锁定笔记",
            "content": "Meilisearch 机密内容",
            "tags": [],
            "created_at": "2026-11-01T00:00:00",
            "updated_at": "2026-11-28T00:00:00",
        },
        4: {
            "note_id": 4,
            "is_locked": False,
            "folder_id": 11,
            "folder_path": "个人资料",
            "alias": "noindex-4",
            "title": "不索引笔记",
            "content": "Meilisearch 排除索引",
            "tags": ["不索引"],
            "created_at": "2026-11-01T00:00:00",
            "updated_at": "2026-11-28T00:00:00",
        },
    }
    meili = FakeNotesMeili()
    svc = HedgeDocNotesService(db_path=str(tmp_path / "archive.db"), source=source, meili=meili)

    ok, stats = svc.sync_notes()
    assert ok is True
    assert stats["upserted"] == 2
    assert stats["excluded_count"] == 2
    assert sorted(meili.docs.keys()) == ["1", "2"]
    assert meili.docs["1"]["url"] == "https://md.maifeipin.com/arch-1"
    assert meili.docs["1"]["headings"] == ["架构设计", "缓存策略"]
    assert meili.docs["2"]["exclude_from_quote"] is True

    ok2, stats2 = svc.sync_notes()
    assert ok2 is True
    assert stats2["upserted"] == 0
    assert stats2["skipped_unchanged"] == 2

    owner_res = svc.search_notes(query="Meilisearch", purpose="search")
    assert [it["note_id"] for it in owner_res["items"]] == [1, 2]
    assert owner_res["items"][1]["exclude_from_quote"] is True

    quote_res = svc.search_notes(query="Meilisearch", purpose="quote")
    assert [it["note_id"] for it in quote_res["items"]] == [1]
    assert "content" in quote_res["items"][0]

    # Already-indexed note 1 gets locked before next sync -> immediately dropped & purged
    source.notes[1]["is_locked"] = True
    after_lock = svc.search_notes(query="Meilisearch", purpose="search")
    assert [it["note_id"] for it in after_lock["items"]] == [2]
    assert after_lock["purged_stale"] == 1
    assert "1" not in meili.docs

    # Already-indexed note 2 gets tagged 不索引 before next sync -> immediately dropped & purged
    source.notes[2]["tags"] = ["不引用", "不索引"]
    after_noindex = svc.search_notes(query="Meilisearch", purpose="search")
    assert after_noindex["items"] == []
    assert after_noindex["purged_stale"] == 1
    assert "2" not in meili.docs


def test_fail_closed_when_live_source_unreachable_and_manual_rules(tmp_path):
    source = FakeHedgeDocSource()
    source.notes[10] = {
        "note_id": 10,
        "is_locked": False,
        "folder_id": 1,
        "folder_path": "08读书笔记",
        "alias": "book-10",
        "title": "思考快与慢",
        "content": "系统1与系统2",
        "tags": [],
        "created_at": "2026-11-01T00:00:00",
        "updated_at": "2026-11-28T00:00:00",
    }
    meili = FakeNotesMeili()
    svc = HedgeDocNotesService(db_path=str(tmp_path / "archive.db"), source=source, meili=meili)
    ok, _ = svc.sync_notes(note_ids=[10])
    assert ok is True
    assert "10" in meili.docs

    source.fail_live_check = True
    fail_res = svc.search_notes(query="系统1", purpose="search")
    assert fail_res["items"] == []
    assert "error" in fail_res
    source.fail_live_check = False

    ok_r, r_res = svc.set_note_rules(10, exclude_from_quote=True)
    assert ok_r is True
    assert r_res["rule"]["exclude_from_quote"] is True
    assert len(svc.search_notes(query="系统1", purpose="search")["items"]) == 1
    assert svc.search_notes(query="系统1", purpose="quote")["items"] == []

    ok_r2, _ = svc.set_note_rules(10, exclude_from_index=True)
    assert ok_r2 is True
    assert "10" not in meili.docs
    assert svc.search_notes(query="系统1", purpose="search")["items"] == []


def test_kb_notes_routes_enforce_owner_only_auth(tmp_path):
    source = FakeHedgeDocSource(preflight_ok=False, preflight_msg="is_locked missing")
    meili = FakeNotesMeili()
    notes = HedgeDocNotesService(
        db_path=str(tmp_path / "archive.db"),
        config={"personal_kb": {"owners": ["owner_user"]}},
        source=source,
        meili=meili,
    )

    for method, url, body in [
        ("GET", "/api/v1/kb/notes?q=test", None),
        ("GET", "/api/v1/kb/notes/preflight", None),
        ("POST", "/api/v1/kb/notes/sync", {}),
        ("PUT", "/api/v1/kb/notes/10/rules", {"exclude_from_quote": True}),
    ]:
        h = _DummyHandler(method=method, body=body, user_session={"username": "other_user"})
        parsed = urlparse(url)
        if parsed.path == "/api/v1/kb/notes":
            kb_routes.handle_get_notes(h, parsed, notes)
        else:
            kb_routes.handle_notes_sub_dispatch(h, parsed, notes)
        assert h.status_code == 403

    h_pre = _DummyHandler(method="GET", user_session={"username": "owner_user"})
    kb_routes.handle_notes_sub_dispatch(h_pre, urlparse("/api/v1/kb/notes/preflight"), notes)
    assert h_pre.status_code == 200
    assert h_pre.json_body()["preflight_ok"] is False

    h_sync = _DummyHandler(method="POST", body={}, user_session={"username": "owner_user"})
    kb_routes.handle_notes_sub_dispatch(h_sync, urlparse("/api/v1/kb/notes/sync"), notes)
    assert h_sync.status_code == 412


def test_extract_markdown_headings_ignores_code_fences():
    md = "# 顶层标题\n```bash\n# shell comment\n```\n## 二级标题\n### 三级标题\n#### 四级忽略"
    assert extract_markdown_headings(md) == ["顶层标题", "二级标题", "三级标题"]

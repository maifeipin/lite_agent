#!/usr/bin/env python3
"""Review room v2: open discussion, traceable rounds, portable archive, human decision.

Standard-library only. Use one SQLite database on the meeting host. No model API
or background service is required; external sessions can participate over SSH.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 2
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data/committee/review_meetings.sqlite3"
NAME = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
POSITIONS = ("support", "revise", "oppose", "abstain")
OPEN = "open"
WAITING = "awaiting_approval"
FINAL = ("approved", "rejected")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_file(path: str) -> str:
    value = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError("文件内容不能为空")
    return value.strip()


def validate_name(name: str) -> str:
    name = name.lower().strip()
    if not NAME.fullmatch(name) or name in {"owner", "human", "system"} or name.startswith("model-"):
        raise ValueError(f"无效参会名称: {name!r}; 使用 2-40 位小写字母、数字、_、-")
    return name


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS meetings (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, brief TEXT NOT NULL,
        state TEXT NOT NULL, round INTEGER NOT NULL, owner_key_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        archived_at INTEGER
      );
      CREATE TABLE IF NOT EXISTS participants (
        meeting_id TEXT NOT NULL REFERENCES meetings(id),
        name TEXT NOT NULL, invited_round INTEGER NOT NULL,
        PRIMARY KEY(meeting_id,name)
      );
      CREATE TABLE IF NOT EXISTS events (
        meeting_id TEXT NOT NULL REFERENCES meetings(id),
        seq INTEGER NOT NULL, round INTEGER NOT NULL, actor TEXT NOT NULL,
        kind TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL,
        prev_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
        PRIMARY KEY(meeting_id,seq)
      );
      CREATE INDEX IF NOT EXISTS events_by_meeting ON events(meeting_id,seq);
    """)
    return db


def get(db: sqlite3.Connection, mid: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
    if row is None:
        raise ValueError(f"未知议题: {mid}")
    return row


def people(db: sqlite3.Connection, mid: str) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT name,invited_round FROM participants WHERE meeting_id=? ORDER BY name", (mid,))]


def events(db: sqlite3.Connection, mid: str, since: int = 0) -> list[dict]:
    result = []
    for row in db.execute("SELECT * FROM events WHERE meeting_id=? AND seq>? ORDER BY seq", (mid, since)):
        item = dict(row)
        item["body"] = json.loads(item["body"])
        result.append(item)
    return result


def check_chain(items: list[dict], mid: str) -> dict:
    prev = "0" * 64
    for index, item in enumerate(items, 1):
        payload = {key: item[key] for key in
                   ("meeting_id", "seq", "round", "actor", "kind", "body", "created_at", "prev_hash")}
        digest = sha(canonical(payload))
        if item["meeting_id"] != mid or item["seq"] != index or item["prev_hash"] != prev or item["event_hash"] != digest:
            return {"valid": False, "failed_seq": index}
        prev = digest
    return {"valid": True, "events": len(items), "head_hash": prev}


def append(db: sqlite3.Connection, row: sqlite3.Row, actor: str, kind: str, body: dict,
           *, round_no: int | None = None, created_at: int | None = None) -> dict:
    last = db.execute("SELECT seq,event_hash FROM events WHERE meeting_id=? ORDER BY seq DESC LIMIT 1", (row["id"],)).fetchone()
    event = {
        "meeting_id": row["id"], "seq": (last["seq"] + 1 if last else 1),
        "round": row["round"] if round_no is None else round_no,
        "actor": actor, "kind": kind, "body": body,
        "created_at": int(time.time()) if created_at is None else created_at,
        "prev_hash": last["event_hash"] if last else "0" * 64,
    }
    event["event_hash"] = sha(canonical(event))
    db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)", (
        event["meeting_id"], event["seq"], event["round"], event["actor"],
        event["kind"], canonical(event["body"]), event["created_at"],
        event["prev_hash"], event["event_hash"],
    ))
    db.execute("UPDATE meetings SET updated_at=? WHERE id=?", (event["created_at"], row["id"]))
    return event


def submitted(db: sqlite3.Connection, mid: str, round_no: int) -> set[str]:
    return {e["actor"] for e in events(db, mid)
            if e["round"] == round_no and e["kind"] == "review"}


def missing(db: sqlite3.Connection, row: sqlite3.Row) -> list[str]:
    done = submitted(db, row["id"], row["round"])
    waived = {name for e in events(db, row["id"])
              if e["round"] == row["round"] and e["kind"] == "attendance_waived"
              for name in e["body"]["names"]}
    return [p["name"] for p in people(db, row["id"])
            if p["invited_round"] <= row["round"] and p["name"] not in done | waived]


def presence(db: sqlite3.Connection, mid: str, round_no: int) -> dict[str, dict]:
    """Report explicit attendance signals without treating them as a vote."""
    result = {p["name"]: {"state": "invited", "event_seq": None}
              for p in people(db, mid)}
    for event in events(db, mid):
        if event["round"] == round_no and event["kind"] in {"joined", "left"} and event["actor"] in result:
            result[event["actor"]] = {
                "state": "present" if event["kind"] == "joined" else "left",
                "event_seq": event["seq"],
            }
    return result


def snapshot(db: sqlite3.Connection, mid: str, *, since: int = 0, full: bool = False) -> dict:
    row = get(db, mid)
    result = {
        "id": mid, "title": row["title"], "state": row["state"],
        "round": row["round"], "participants": people(db, mid),
        "missing": missing(db, row), "presence": presence(db, mid, row["round"]),
        "archived_at": row["archived_at"],
    }
    if full:
        if since == 0:
            result["brief"] = row["brief"]
        result["events"] = events(db, mid, since)
        result["since"] = since
    else:
        result["last_seq"] = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE meeting_id=?", (mid,)).fetchone()[0]
    return result


def require_open(row: sqlite3.Row) -> None:
    if row["state"] != OPEN or row["archived_at"] is not None:
        raise ValueError("议题未处于开放讨论状态")


def require_person(db: sqlite3.Connection, row: sqlite3.Row, actor: str) -> str:
    actor = validate_name(actor)
    names = {p["name"] for p in people(db, row["id"]) if p["invited_round"] <= row["round"]}
    if actor not in names:
        raise ValueError(f"{actor} 尚未获邀参与当前轮次")
    return actor


def check_owner(row: sqlite3.Row, *, key_file: str | None, token_stdin: bool,
                confirm: str, decision: str) -> None:
    if confirm != f"{decision}:{row['id']}":
        raise ValueError("人工确认串不匹配")
    if token_stdin:
        token = sys.stdin.readline().strip()
    else:
        key = Path(key_file)
        if key.stat().st_mode & 0o077:
            raise ValueError("审批凭据文件必须只允许所有者读取（chmod 600）")
        token = key.read_text(encoding="utf-8").strip()
    if not secrets.compare_digest(sha(token), row["owner_key_hash"]):
        raise ValueError("审批凭据不匹配")


def new_owner_key(path: str) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")
    return sha(token)


def export_bundle(db: sqlite3.Connection, mid: str) -> dict:
    row = get(db, mid)
    bundle = {
        "format": "lite_agent.review_meeting", "schema_version": SCHEMA_VERSION,
        "meeting": {k: row[k] for k in ("id", "title", "brief", "state", "round", "owner_key_hash", "created_at", "updated_at", "archived_at")},
        "participants": people(db, mid), "events": events(db, mid),
    }
    bundle["chain"] = check_chain(bundle["events"], mid)
    if not bundle["chain"]["valid"]:
        raise ValueError("审计链无效，拒绝导出")
    bundle["bundle_sha256"] = sha(canonical(bundle))
    return bundle


def import_bundle(db: sqlite3.Connection, bundle: dict) -> dict:
    if bundle.get("format") != "lite_agent.review_meeting" or bundle.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("不支持的导出格式或版本")
    expected = bundle.get("bundle_sha256")
    actual = sha(canonical({k: v for k, v in bundle.items() if k != "bundle_sha256"}))
    if expected != actual:
        raise ValueError("导出包校验和不匹配")
    m = bundle["meeting"]
    if not check_chain(bundle["events"], m["id"])["valid"]:
        raise ValueError("审计链无效，拒绝导入")
    if db.execute("SELECT 1 FROM meetings WHERE id=?", (m["id"],)).fetchone():
        raise ValueError("议题 ID 已存在，拒绝覆盖")
    db.execute("INSERT INTO meetings VALUES(?,?,?,?,?,?,?,?,?)", tuple(m[k] for k in
        ("id", "title", "brief", "state", "round", "owner_key_hash", "created_at", "updated_at", "archived_at")))
    for p in bundle["participants"]:
        db.execute("INSERT INTO participants VALUES(?,?,?)", (m["id"], p["name"], p["invited_round"]))
    for e in bundle["events"]:
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)", (
            e["meeting_id"], e["seq"], e["round"], e["actor"], e["kind"],
            canonical(e["body"]), e["created_at"], e["prev_hash"], e["event_hash"]))
    return {"id": m["id"], "imported_events": len(bundle["events"]), "state": m["state"]}


def import_legacy(db: sqlite3.Connection, old: dict, owner_key_hash: str) -> dict:
    """Migrate a v1 `show` JSON snapshot after verifying its original hash chain."""
    mid = old["id"]
    if db.execute("SELECT 1 FROM meetings WHERE id=?", (mid,)).fetchone():
        raise ValueError("议题 ID 已存在，拒绝覆盖")
    if not re.fullmatch(r"[0-9a-f]{64}", owner_key_hash):
        raise ValueError("owner-key-hash 必须是 64 位小写 SHA-256")
    previous = "0" * 64
    for source in old["entries"]:
        signed = {k: source[k] for k in
                  ("meeting_id", "round", "actor", "kind", "body", "created_at", "prev_hash")}
        digest = sha(canonical(signed))
        if source["meeting_id"] != mid or source["prev_hash"] != previous or source["entry_hash"] != digest:
            raise ValueError("v1 审计链无效，拒绝导入")
        previous = digest
    roster = [validate_name(name) for name in old["participants"]]
    if len(roster) != len(set(roster)) or not roster:
        raise ValueError("v1 参会名单无效")
    state = old["state"]
    if state not in {OPEN, WAITING, "approved", "rejected", "changes_requested"}:
        raise ValueError("v1 议题状态无效")
    db.execute("INSERT INTO meetings VALUES(?,?,?,?,?,?,?,?,?)", (
        mid, old["title"], old["brief"], state, old["current_round"],
        owner_key_hash, old["created_at"], old["updated_at"], None))
    for name in roster:
        db.execute("INSERT INTO participants VALUES(?,?,?)", (mid, name, 1))
    actor_history: dict[str, list[tuple[int, int]]] = {}
    for source in old["entries"]:
        body = source["body"]
        if source["kind"] == "review":
            prior = [seq for round_no, seq in actor_history.get(body.get("responds_to"), [])
                     if round_no < source["round"]]
            reference = prior[-1] if prior else None
            mapped = {"position": body["position"], "text": body["review"],
                      "responds_to_seq": reference, "legacy_entry_hash": source["entry_hash"]}
        else:
            mapped = {"legacy_body": body, "legacy_entry_hash": source["entry_hash"]}
        event = append(db, get(db, mid), source["actor"], source["kind"], mapped,
                       round_no=source["round"], created_at=source["created_at"])
        if source["kind"] in {"review", "model_vote", "prior_model_vote"}:
            actor_history.setdefault(source["actor"], []).append((source["round"], event["seq"]))
    db.execute("UPDATE meetings SET updated_at=? WHERE id=?", (int(time.time()), mid))
    return {"id": mid, "imported_events": len(old["entries"]), "state": state,
            "legacy_head_hash": previous, "new_chain": check_chain(events(db, mid), mid)}


def execute(db: sqlite3.Connection, a: argparse.Namespace) -> dict:
    if a.action == "create":
        names = [validate_name(x) for x in a.participants.split(",") if x.strip()]
        if not names or len(names) != len(set(names)):
            raise ValueError("参会名单必须非空且不重复")
        brief = read_file(a.brief_file)
        key_hash = a.owner_key_hash or new_owner_key(a.owner_key_file)
        if not re.fullmatch(r"[0-9a-f]{64}", key_hash):
            raise ValueError("owner-key-hash 必须是 64 位小写 SHA-256")
        now = int(time.time())
        mid = uuid.uuid4().hex[:12]
        db.execute("INSERT INTO meetings VALUES(?,?,?,?,?,?,?,?,?)", (mid, a.title, brief, OPEN, 1, key_hash, now, now, None))
        for name in names:
            db.execute("INSERT INTO participants VALUES(?,?,?)", (mid, name, 1))
        append(db, get(db, mid), "system", "created", {"brief_sha256": sha(brief), "participants": names})
        return {**snapshot(db, mid), "owner_key_file": a.owner_key_file}
    if a.action == "list":
        return {"meetings": [dict(r) for r in db.execute(
            "SELECT id,title,state,round,updated_at,archived_at FROM meetings ORDER BY updated_at DESC LIMIT ?", (a.limit,))]}
    if a.action == "import":
        return import_bundle(db, json.loads(read_file(a.file)))
    if a.action == "import-legacy":
        key_hash = a.owner_key_hash or new_owner_key(a.owner_key_file)
        return import_legacy(db, json.loads(read_file(a.file)), key_hash)

    row = get(db, a.id)
    mid = row["id"]
    if a.action == "status":
        return snapshot(db, mid)
    if a.action == "show":
        return snapshot(db, mid, since=a.since, full=True)
    if a.action == "verify":
        return {"id": mid, **check_chain(events(db, mid), mid)}
    if a.action == "export":
        bundle = export_bundle(db, mid)
        target = Path(a.file)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
        return {"id": mid, "file": str(target), "events": len(bundle["events"]), "sha256": bundle["bundle_sha256"]}
    if a.action == "invite":
        require_open(row)
        actor = validate_name(a.participant)
        if db.execute("SELECT 1 FROM participants WHERE meeting_id=? AND name=?", (mid, actor)).fetchone():
            raise ValueError("参会者已在名单中")
        db.execute("INSERT INTO participants VALUES(?,?,?)", (mid, actor, row["round"]))
        append(db, row, "system", "invited", {"name": actor, "from_round": row["round"]})
        return snapshot(db, mid)
    if a.action == "guide":
        actor = require_person(db, row, a.participant)
        return {"id": mid, "participant": actor, "round": row["round"],
                "state": row["state"], "brief_sha256": sha(row["brief"]),
                "steps": [f"python3 scripts/review_meeting.py join --id {mid} --participant {actor}",
                          f"python3 scripts/review_meeting.py show --id {mid} --since 0",
                          f"python3 scripts/review_meeting.py submit --id {mid} --participant {actor} --position revise --file review.md"
                          + (" --responds-to <prior_event_seq>" if row["round"] >= 2 else ""),
                          f"python3 scripts/review_meeting.py leave --id {mid} --participant {actor}"],
                "rule": "先阅读原始提案和其他评委原文；不得代用户调用 decide；身份名称由使用者自报。"}
    if a.action in {"join", "leave"}:
        require_open(row)
        actor = require_person(db, row, a.participant)
        current = presence(db, mid, row["round"])[actor]["state"]
        if a.action == "join" and current == "present":
            return {"id": mid, "participant": actor, "state": current, "changed": False}
        if a.action == "leave" and current != "present":
            raise ValueError("参会者尚未入会，无法离会")
        event = append(db, row, actor, "joined" if a.action == "join" else "left", {})
        return {"id": mid, "participant": actor,
                "state": "present" if a.action == "join" else "left",
                "changed": True, "event_seq": event["seq"]}
    if a.action == "comment":
        require_open(row)
        actor = require_person(db, row, a.participant)
        ref = a.responds_to
        if ref is not None:
            found = db.execute("SELECT 1 FROM events WHERE meeting_id=? AND seq=?", (mid, ref)).fetchone()
            if not found:
                raise ValueError("被回复的事件不存在")
        event = append(db, row, actor, "comment", {"text": read_file(a.file), "responds_to_seq": ref})
        return {"id": mid, "event_seq": event["seq"], "round": row["round"]}
    if a.action == "submit":
        require_open(row)
        actor = require_person(db, row, a.participant)
        if actor in submitted(db, mid, row["round"]):
            raise ValueError("本轮已提交正式意见；后续交流请使用 comment")
        ref = a.responds_to
        if row["round"] >= 2:
            source = db.execute("SELECT actor,kind FROM events WHERE meeting_id=? AND seq=? AND round<?", (mid, ref, row["round"])).fetchone() if ref else None
            if source is None or source["actor"] == actor or source["kind"] not in {"review", "comment"}:
                raise ValueError("第 2 轮起必须回应另一位评委此前的具体发言序号")
        event = append(db, row, actor, "review", {"position": a.position, "text": read_file(a.file), "responds_to_seq": ref})
        return {"id": mid, "event_seq": event["seq"], "round": row["round"], "missing": missing(db, row)}
    if a.action == "import-audit":
        require_open(row)
        audit = json.loads(read_file(a.file))
        same = audit.get("brief", "").strip() == row["brief"].strip()
        imported = []
        for name, result in audit.get("results", {}).items():
            safe_name = re.sub(r"[^a-z0-9_-]", "-", str(name).lower()).strip("-")
            if not safe_name:
                raise ValueError("审计模型名称无效")
            actor = "model-" + safe_name
            append(db, row, actor, "model_vote" if same else "prior_model_vote", {
                "source_run_id": audit.get("run_id"), "source_audit_sha256": sha(canonical(audit)),
                "same_brief": same, "result": result})
            imported.append(actor)
        if not imported:
            raise ValueError("审计文件没有模型结果")
        return {"id": mid, "imported": imported, "same_brief": same}
    if a.action == "waive":
        require_open(row)
        names = [validate_name(x) for x in a.participants.split(",") if x.strip()]
        pending = set(missing(db, row))
        if not names or not set(names) <= pending or not a.reason.strip():
            raise ValueError("只能对当前未提交者注明缺席，并填写原因")
        append(db, row, "system", "attendance_waived", {"names": names, "reason": a.reason.strip()})
        return snapshot(db, mid)
    if a.action == "advance":
        require_open(row)
        if missing(db, row):
            raise ValueError("仍有未提交参会者；请先等待或用 waive 记录缺席")
        append(db, row, "system", "round_closed", {})
        db.execute("UPDATE meetings SET round=round+1 WHERE id=?", (mid,))
        return snapshot(db, mid)
    if a.action == "request-approval":
        require_open(row)
        if row["round"] < 2 or missing(db, row):
            raise ValueError("至少进入第二轮并处理所有未提交者后才能申请审批")
        all_events = events(db, mid)
        voices = {e["actor"] for e in all_events if e["kind"] == "review"}
        replies = [e for e in all_events if e["round"] >= 2 and e["kind"] == "review" and e["body"].get("responds_to_seq")]
        if len(voices) < 2 or not replies:
            raise ValueError("至少需要两位独立评委和一条第 2 轮交叉回应")
        summary = read_file(a.summary_file)
        append(db, row, "system", "approval_requested", {"summary": summary, "summary_sha256": sha(summary)})
        db.execute("UPDATE meetings SET state=? WHERE id=?", (WAITING, mid))
        return snapshot(db, mid)
    if a.action == "decide":
        if row["state"] != WAITING or row["archived_at"] is not None:
            raise ValueError("只有待审批议题可由人工作出决定")
        check_owner(row, key_file=a.owner_key_file, token_stdin=a.owner_token_stdin,
                    confirm=a.confirm, decision=a.decision)
        note = read_file(a.note_file)
        append(db, row, "human", "decision", {"decision": a.decision, "note": note})
        next_state = {"approve": "approved", "revise": "changes_requested", "reject": "rejected"}[a.decision]
        db.execute("UPDATE meetings SET state=? WHERE id=?", (next_state, mid))
        return snapshot(db, mid)
    if a.action == "resume":
        if row["state"] != "changes_requested" or row["archived_at"] is not None:
            raise ValueError("只有要求修改的议题可恢复讨论")
        new_brief = read_file(a.brief_file)
        if new_brief == row["brief"]:
            raise ValueError("修改后提案必须与上一版不同")
        append(db, row, "system", "revision_started", {
            "previous_round": row["round"], "previous_brief": row["brief"],
            "previous_brief_sha256": sha(row["brief"]),
            "new_brief": new_brief, "new_brief_sha256": sha(new_brief),
        })
        db.execute("UPDATE meetings SET brief=? WHERE id=?", (new_brief, mid))
        db.execute("UPDATE meetings SET state=?,round=round+1 WHERE id=?", (OPEN, mid))
        return snapshot(db, mid)
    if a.action == "archive":
        if row["state"] not in FINAL or row["archived_at"] is not None:
            raise ValueError("只能归档已批准或已拒绝且尚未归档的议题")
        append(db, row, "system", "archived", {})
        db.execute("UPDATE meetings SET archived_at=? WHERE id=?", (int(time.time()), mid))
        return snapshot(db, mid)
    raise ValueError("未知命令")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=Path(os.environ.get("REVIEW_MEETING_DB", DEFAULT_DB)))
    sub = p.add_subparsers(dest="action", required=True)
    c = sub.add_parser("create")
    c.add_argument("--title", required=True)
    c.add_argument("--brief-file", required=True)
    c.add_argument("--participants", required=True)
    owner_create = c.add_mutually_exclusive_group(required=True)
    owner_create.add_argument("--owner-key-file", help="在本机生成 0600 审批凭据；不要交给参会 AI")
    owner_create.add_argument("--owner-key-hash", help="只存人类本地凭据的 SHA-256；共享 VPS 推荐此方式")
    l = sub.add_parser("list")
    l.add_argument("--limit", type=int, default=20)
    imp = sub.add_parser("import")
    imp.add_argument("--file", required=True)
    old = sub.add_parser("import-legacy")
    old.add_argument("--file", required=True, help="v1 show 输出的完整 JSON")
    old_owner = old.add_mutually_exclusive_group(required=True)
    old_owner.add_argument("--owner-key-file")
    old_owner.add_argument("--owner-key-hash")
    for name in ("status", "show", "verify", "export", "invite", "guide", "join", "leave", "comment", "submit",
                 "import-audit", "waive", "advance", "request-approval", "decide", "resume", "archive"):
        s = sub.add_parser(name)
        s.add_argument("--id", required=True)
        if name == "show":
            s.add_argument("--since", type=int, default=0)
        if name in {"export", "comment", "submit", "import-audit"}:
            s.add_argument("--file", required=True)
        if name in {"invite", "guide", "join", "leave", "comment", "submit"}:
            s.add_argument("--participant", required=True)
        if name in {"comment", "submit"}:
            s.add_argument("--responds-to", type=int)
        if name == "submit":
            s.add_argument("--position", choices=POSITIONS, required=True)
        if name == "waive":
            s.add_argument("--participants", required=True)
            s.add_argument("--reason", required=True)
        if name == "request-approval":
            s.add_argument("--summary-file", required=True)
        if name == "resume":
            s.add_argument("--brief-file", required=True, help="人工要求修改后的新版完整提案")
        if name == "decide":
            s.add_argument("--decision", choices=("approve", "revise", "reject"), required=True)
            s.add_argument("--note-file", required=True)
            owner_decide = s.add_mutually_exclusive_group(required=True)
            owner_decide.add_argument("--owner-key-file", help="本机 0600 审批凭据")
            owner_decide.add_argument("--owner-token-stdin", action="store_true", help="从标准输入读取一行审批凭据；避免放入命令参数")
            s.add_argument("--confirm", required=True, help="<decision>:<id>")
    return p


def main() -> int:
    a = parser().parse_args()
    try:
        with connect(a.db) as db:
            db.execute("BEGIN IMMEDIATE")
            result = execute(db, a)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

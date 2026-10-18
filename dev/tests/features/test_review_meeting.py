import json
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import review_meeting as room


class ReviewMeetingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = room.connect(self.root / "room.sqlite3")
        self.addCleanup(self.db.close)
        self.brief = self._file("brief.md", "Review meeting v2 implementation proposal")
        self.owner = str(self.root / "owner.key")

    def _file(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def act(self, action, **kwargs):
        with self.db:
            return room.execute(self.db, SimpleNamespace(action=action, **kwargs))

    def create(self):
        return self.act("create", title="Review room v2", brief_file=self.brief,
                        participants="codex,antigravity,trae", owner_key_file=self.owner,
                        owner_key_hash=None)["id"]

    def test_full_lifecycle_export_import_and_archive(self):
        mid = self.create()
        for name in ("cursor", "qwen", "workbuddy"):
            self.act("invite", id=mid, participant=name)
        self.assertEqual(len(self.act("status", id=mid)["missing"]), 6)

        review_file = self._file("review.md", "I support this with a recorded caveat.")
        codex = self.act("submit", id=mid, participant="codex", position="revise",
                         file=review_file, responds_to=None)
        self.assertEqual(codex["event_seq"], 5)
        with self.assertRaisesRegex(ValueError, "已提交"):
            self.act("submit", id=mid, participant="codex", position="support",
                     file=review_file, responds_to=None)
        self.act("comment", id=mid, participant="cursor", file=review_file,
                 responds_to=codex["event_seq"])
        delta = room.snapshot(self.db, mid, since=codex["event_seq"], full=True)
        self.assertNotIn("brief", delta)
        self.assertEqual(len(delta["events"]), 1)
        self.act("submit", id=mid, participant="antigravity", position="support",
                 file=review_file, responds_to=None)
        self.act("waive", id=mid, participants="trae,cursor,qwen,workbuddy",
                 reason="Invited, no response before deadline")
        self.assertEqual(self.act("status", id=mid)["missing"], [])
        self.act("advance", id=mid)
        self.assertEqual(self.act("status", id=mid)["round"], 2)
        self.assertEqual(self.act("status", id=mid)["presence"]["codex"]["state"], "invited")

        with self.assertRaisesRegex(ValueError, "具体发言"):
            self.act("submit", id=mid, participant="codex", position="support",
                     file=review_file, responds_to=None)
        self.act("submit", id=mid, participant="codex", position="support",
                 file=review_file, responds_to=codex["event_seq"] + 2)
        self.act("waive", id=mid, participants="antigravity,trae,cursor,qwen,workbuddy",
                 reason="Absent from second round")
        summary = self._file("summary.md", "Codex and Antigravity reviewed; others absent. Risks remain.")
        self.act("request-approval", id=mid, summary_file=summary)
        self.assertEqual(self.act("status", id=mid)["state"], "awaiting_approval")
        with self.assertRaisesRegex(ValueError, "确认串"):
            self.act("decide", id=mid, owner_key_file=self.owner,
                     owner_token_stdin=False, confirm="wrong", decision="approve", note_file=summary)
        self.act("decide", id=mid, owner_key_file=self.owner,
                 owner_token_stdin=False, confirm=f"approve:{mid}", decision="approve", note_file=summary)
        self.act("archive", id=mid)
        self.assertTrue(self.act("verify", id=mid)["valid"])
        export_file = str(self.root / "export.json")
        self.act("export", id=mid, file=export_file)
        self.assertEqual(Path(export_file).stat().st_mode & 0o777, 0o600)
        other = room.connect(self.root / "other.sqlite3")
        try:
            with other:
                room.import_bundle(other, json.loads(Path(export_file).read_text()))
            self.assertEqual(room.snapshot(other, mid)["state"], "approved")
            self.assertTrue(room.check_chain(room.events(other, mid), mid)["valid"])
            with self.assertRaisesRegex(ValueError, "已存在"):
                with other:
                    room.import_bundle(other, json.loads(Path(export_file).read_text()))
        finally:
            other.close()

    def test_owner_can_cancel_unfinished_meeting_without_review_votes(self):
        mid = self.create()
        note = self._file("cancel.md", "Smoke cleanup; no substantive decision")
        with self.assertRaisesRegex(ValueError, "确认串"):
            self.act("cancel", id=mid, owner_key_file=self.owner, owner_token_stdin=False,
                     confirm=f"reject:{mid}", note_file=note)
        result = self.act("cancel", id=mid, owner_key_file=self.owner, owner_token_stdin=False,
                          confirm=f"cancel:{mid}", note_file=note)
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(room.events(self.db, mid)[-1]["kind"], "cancelled")
        self.assertFalse(any(e["kind"] in {"review", "approval_requested", "decision"}
                             for e in room.events(self.db, mid)))
        self.act("archive", id=mid)
        self.assertTrue(self.act("verify", id=mid)["valid"])
        with self.assertRaisesRegex(ValueError, "未裁决"):
            self.act("cancel", id=mid, owner_key_file=self.owner, owner_token_stdin=False,
                     confirm=f"cancel:{mid}", note_file=note)

    def test_tampered_export_is_rejected(self):
        mid = self.create()
        bundle = room.export_bundle(self.db, mid)
        bundle["meeting"]["brief"] = "tampered"
        with self.assertRaisesRegex(ValueError, "校验和"):
            room.import_bundle(room.connect(self.root / "bad.sqlite3"), bundle)

    def test_join_leave_are_audited_without_counting_as_review(self):
        mid = self.create()
        joined = self.act("join", id=mid, participant="codex")
        self.assertTrue(joined["changed"])
        self.assertEqual(self.act("join", id=mid, participant="codex")["changed"], False)
        self.assertEqual(self.act("status", id=mid)["presence"]["codex"]["state"], "present")
        self.assertIn("codex", self.act("status", id=mid)["missing"])
        self.act("leave", id=mid, participant="codex")
        self.assertEqual(self.act("status", id=mid)["presence"]["codex"]["state"], "left")
        with self.assertRaisesRegex(ValueError, "尚未入会"):
            self.act("leave", id=mid, participant="codex")
        self.assertTrue(self.act("verify", id=mid)["valid"])

    def test_model_audit_different_brief_is_history(self):
        mid = self.create()
        audit = self._file("audit.json", json.dumps({
            "run_id": "old-run", "brief": "different", "results": {"glm-5.2": {"score": 80}}
        }))
        result = self.act("import-audit", id=mid, file=audit)
        self.assertFalse(result["same_brief"])
        self.assertEqual(room.events(self.db, mid)[-1]["kind"], "prior_model_vote")
        self.assertEqual(room.events(self.db, mid)[-1]["actor"], "model-glm-5-2")
        review = self._file("r.md", "first round")
        for name in ("codex", "antigravity", "trae"):
            self.act("submit", id=mid, participant=name, position="support",
                     file=review, responds_to=None)
        self.act("advance", id=mid)
        with self.assertRaisesRegex(ValueError, "具体发言"):
            self.act("submit", id=mid, participant="codex", position="support",
                     file=review, responds_to=2)

    def test_revise_requires_new_brief_and_keeps_both_versions(self):
        mid = self.create()
        note = self._file("r.md", "reviewed")
        first = self.act("submit", id=mid, participant="codex", position="revise",
                         file=note, responds_to=None)["event_seq"]
        self.act("submit", id=mid, participant="antigravity", position="support",
                 file=note, responds_to=None)
        self.act("waive", id=mid, participants="trae", reason="absent")
        self.act("advance", id=mid)
        self.act("submit", id=mid, participant="antigravity", position="revise",
                 file=note, responds_to=first)
        self.act("waive", id=mid, participants="codex,trae", reason="absent")
        self.act("request-approval", id=mid, summary_file=note)
        self.act("decide", id=mid, owner_key_file=self.owner,
                 owner_token_stdin=False, confirm=f"revise:{mid}", decision="revise", note_file=note)
        with self.assertRaisesRegex(ValueError, "必须与上一版不同"):
            self.act("resume", id=mid, brief_file=self.brief)
        amended = self._file("amended.md", "Review meeting v2 proposal, revised")
        result = self.act("resume", id=mid, brief_file=amended)
        self.assertEqual(result["round"], 3)
        self.assertEqual(room.snapshot(self.db, mid, full=True)["brief"], "Review meeting v2 proposal, revised")
        revision = room.events(self.db, mid)[-1]
        self.assertEqual(revision["body"]["previous_brief"], "Review meeting v2 implementation proposal")
        self.assertEqual(revision["body"]["new_brief"], "Review meeting v2 proposal, revised")
        self.assertTrue(self.act("verify", id=mid)["valid"])

    def test_remote_approval_uses_hash_without_remote_key_file(self):
        token = "human-held-secret"
        mid = self.act("create", title="Remote", brief_file=self.brief,
                       participants="codex,trae", owner_key_file=None,
                       owner_key_hash=room.sha(token))["id"]
        self.assertFalse(Path(self.owner).exists())
        review = self._file("r.md", "reviewed")
        first = self.act("submit", id=mid, participant="codex", position="support",
                         file=review, responds_to=None)["event_seq"]
        self.act("submit", id=mid, participant="trae", position="support",
                 file=review, responds_to=None)
        self.act("advance", id=mid)
        self.act("submit", id=mid, participant="trae", position="support",
                 file=review, responds_to=first)
        self.act("waive", id=mid, participants="codex", reason="No response")
        self.act("request-approval", id=mid, summary_file=review)
        with patch("sys.stdin", io.StringIO("wrong\n")):
            with self.assertRaisesRegex(ValueError, "凭据不匹配"):
                self.act("decide", id=mid, owner_key_file=None, owner_token_stdin=True,
                         confirm=f"approve:{mid}", decision="approve", note_file=review)
        with patch("sys.stdin", io.StringIO(token + "\n")):
            result = self.act("decide", id=mid, owner_key_file=None, owner_token_stdin=True,
                              confirm=f"approve:{mid}", decision="approve", note_file=review)
        self.assertEqual(result["state"], "approved")

    def test_legacy_v1_snapshot_can_resume_in_round_two(self):
        mid = "legacy123456"
        old = {"id": mid, "title": "Old meeting", "brief": "old brief",
               "participants": ["codex", "trae"], "state": "open",
               "current_round": 2, "created_at": 1, "updated_at": 4, "entries": []}
        prev = "0" * 64
        for seq, actor, kind, body in (
            (41, "owner", "created", {"title": "Old meeting"}),
            (42, "codex", "review", {"position": "revise", "review": "first", "responds_to": None}),
            (43, "trae", "review", {"position": "support", "review": "first", "responds_to": None}),
            (44, "owner", "round_closed", {"missing": []}),
        ):
            event = {"meeting_id": mid, "round": 1, "actor": actor, "kind": kind,
                     "body": body, "created_at": seq, "prev_hash": prev}
            digest = room.sha(room.canonical(event))
            old["entries"].append({"seq": seq, **event, "entry_hash": digest})
            prev = digest
        result = room.import_legacy(self.db, old, room.sha("owner-token"))
        self.assertTrue(result["new_chain"]["valid"])
        self.assertEqual(self.act("status", id=mid)["missing"], ["codex", "trae"])
        note = self._file("legacy_reply.md", "I respond to Trae's first-round position")
        self.act("submit", id=mid, participant="codex", position="support",
                 file=note, responds_to=3)
        self.assertTrue(self.act("verify", id=mid)["valid"])


if __name__ == "__main__":
    unittest.main()

import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from channels.api import ApiHandler
from core import review_meeting_api as access
from scripts import review_meeting as room


class ReviewMeetingAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "meetings.sqlite3"
        self.env = patch.dict(os.environ, {"REVIEW_MEETING_DB": str(self.db_path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        class QuietHandler(ApiHandler):
            def log_message(self, _format, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.addCleanup(self.server.server_close)
        self.server.api_server = SimpleNamespace(
            auth_token="admin-secret",
            config={"guest_token": "guest-secret",
                    "review_meeting_base_url": f"http://127.0.0.1:{self.server.server_port}"},
            port=self.server.server_port,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}/agent/api/v1/review-meetings"

    def request(self, method, path="", body=None, token=None):
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def create(self):
        return self.request("POST", body={
            "title": "Code review", "brief": "Review local diff and discuss.",
            "owner_key_hash": room.sha("human-only-secret")}, token="admin-secret")

    def test_shared_invite_scoped_sessions_and_audit_chain(self):
        self.assertEqual(self.request("POST", body={}, token="guest-secret")[0], 403)
        status, created = self.create()
        self.assertEqual(status, 201)
        mid = created["id"]
        self.assertIn("#invite=", created["invite_url"])
        self.assertNotIn("human-only-secret", created["invite_url"])
        invite = created["invite_url"].split("#invite=", 1)[1]
        self.assertEqual(self.request("GET", f"/{mid}/invite")[0], 200)
        self.assertEqual(self.request("POST", f"/{mid}/join", {"invite_token": "bad"})[0], 401)
        status, first = self.request("POST", f"/{mid}/join", {"invite_token": invite, "label": "Cursor"})
        self.assertEqual(status, 201)
        self.assertEqual(first["participant"], "cursor")
        status, second = self.request("POST", f"/{mid}/join", {"invite_token": invite, "label": "Cursor"})
        self.assertEqual(status, 201)
        self.assertEqual(second["participant"], "cursor-2")
        token = first["session_token"]
        self.assertEqual(self.request("GET", f"/{mid}", token=invite)[0], 401)
        self.assertEqual(self.request("GET", f"/{mid}", token=token)[0], 200)
        self.assertEqual(self.request("POST", f"/{mid}/reviews", {
            "position": "revise", "text": "Line 42 needs a guard."}, token=token)[0], 201)
        self.assertEqual(self.request("POST", f"/{mid}/reviews", {
            "position": "support", "text": "Repeated vote"}, token=token)[0], 400)
        self.assertEqual(self.request("POST", f"/{mid}/comments", {
            "text": "Please check the test fixture."}, token=token)[0], 201)
        self.assertEqual(self.request("GET", f"/{mid}", token="guest-secret")[0], 401)
        with closing(room.connect(self.db_path)) as db:
            self.assertTrue(room.check_chain(room.events(db, mid), mid)["valid"])
            bundle = room.export_bundle(db, mid)
            exported = json.dumps(bundle)
            self.assertNotIn(invite, exported)
            self.assertNotIn(token, exported)
        self.assertEqual(self.request("POST", f"/{mid}/leave", token=token)[0], 200)
        self.assertEqual(self.request("GET", f"/{mid}", token=token)[0], 401)

    def test_expiry_cross_meeting_and_closed_room(self):
        _, first = self.create()
        _, second = self.create()
        invite = first["invite_url"].split("#invite=", 1)[1]
        mid = first["id"]
        other = second["id"]
        self.assertEqual(self.request("POST", f"/{other}/join", {
            "invite_token": invite, "label": "qwen"})[0], 401)
        _, joined = self.request("POST", f"/{mid}/join", {"invite_token": invite, "label": "qwen"})
        session = joined["session_token"]
        self.assertEqual(self.request("GET", f"/{other}", token=session)[0], 401)
        with closing(room.connect(self.db_path)) as db:
            with db:
                db.execute("UPDATE meeting_invites SET expires_at=0 WHERE meeting_id=?", (mid,))
        self.assertEqual(self.request("POST", f"/{mid}/join", {
            "invite_token": invite, "label": "trae"})[0], 401)
        self.assertEqual(self.request("GET", f"/{mid}", token=session)[0], 200)
        with closing(room.connect(self.db_path)) as db:
            with db:
                db.execute("UPDATE meeting_sessions SET expires_at=0 WHERE meeting_id=?", (mid,))
        self.assertEqual(self.request("GET", f"/{mid}", token=session)[0], 401)
        with closing(room.connect(self.db_path)) as db:
            with db:
                db.execute("UPDATE meetings SET state='awaiting_approval' WHERE id=?", (mid,))
        self.assertEqual(self.request("POST", f"/{mid}/join", {
            "invite_token": invite, "label": "trae"})[0], 403)

    def test_token_has_no_access_to_general_api_or_admin_actions(self):
        _, created = self.create()
        mid = created["id"]
        invite = created["invite_url"].split("#invite=", 1)[1]
        _, joined = self.request("POST", f"/{mid}/join", {"invite_token": invite})
        session = joined["session_token"]
        self.assertEqual(self.request("POST", body={}, token=invite)[0], 403)
        self.assertEqual(self.request("POST", body={}, token=session)[0], 403)
        self.assertEqual(self.request("POST", f"/{mid}/invites", {}, token=session)[0], 403)
        generic = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/v1/chat",
            data=b'{}', headers={"Authorization": f"Bearer {session}"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(generic, timeout=5)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_second_round_and_human_approval_boundary(self):
        _, created = self.create()
        mid = created["id"]
        invite = created["invite_url"].split("#invite=", 1)[1]
        _, codex = self.request("POST", f"/{mid}/join", {"invite_token": invite, "label": "codex"})
        _, qwen = self.request("POST", f"/{mid}/join", {"invite_token": invite, "label": "qwen"})
        codex_token = codex["session_token"]
        qwen_token = qwen["session_token"]
        _, first = self.request("POST", f"/{mid}/reviews", {
            "position": "revise", "text": "Need an error case"}, token=codex_token)
        self.assertEqual(self.request("POST", f"/{mid}/reviews", {
            "position": "support", "text": "The design is workable"}, token=qwen_token)[0], 201)
        with closing(room.connect(self.db_path)) as db:
            with db:
                result = room.execute(db, SimpleNamespace(action="advance", id=mid))
            self.assertEqual(result["round"], 2)
        self.assertEqual(self.request("POST", f"/{mid}/reviews", {
            "position": "support", "text": "I changed my view"}, token=qwen_token)[0], 400)
        self.assertEqual(self.request("POST", f"/{mid}/reviews", {
            "position": "revise", "text": "Addressing Codex's error case",
            "responds_to_seq": first["event_seq"]}, token=qwen_token)[0], 201)
        with closing(room.connect(self.db_path)) as db:
            with db:
                room.execute(db, SimpleNamespace(
                    action="waive", id=mid, participants="codex", reason="No second-round response"))
                summary = Path(self.temp.name) / "summary.md"
                summary.write_text("Human decision needed", encoding="utf-8")
                result = room.execute(db, SimpleNamespace(
                    action="request-approval", id=mid, summary_file=str(summary)))
            self.assertEqual(result["state"], "awaiting_approval")
        self.assertEqual(self.request("GET", f"/{mid}", token=qwen_token)[0], 403)

    def test_explicit_invite_ttl_and_rotation(self):
        _, created = self.create()
        mid = created["id"]
        old = created["invite_url"].split("#invite=", 1)[1]
        _, joined = self.request("POST", f"/{mid}/join", {
            "invite_token": old, "label": "codex"})
        old_session = joined["session_token"]
        self.assertEqual(self.request("POST", f"/{mid}/invites", {
            "invite_ttl_seconds": 1}, token="admin-secret")[0], 400)
        status, rotated = self.request("POST", f"/{mid}/invites", {
            "invite_ttl_seconds": 300}, token="admin-secret")
        self.assertEqual(status, 201)
        fresh = rotated["invite_url"].split("#invite=", 1)[1]
        self.assertNotEqual(old, fresh)
        self.assertEqual(self.request("GET", f"/{mid}", token=old_session)[0], 401)
        self.assertEqual(self.request("POST", f"/{mid}/join", {
            "invite_token": old})[0], 401)
        status, resumed = self.request("POST", f"/{mid}/join", {
            "invite_token": fresh, "label": "codex"})
        self.assertEqual(status, 201)
        self.assertEqual(resumed["participant"], "codex")

    def _raw_get_with_host(self, host_header, path=""):
        """GET a meeting endpoint with an explicit Host header via http.client.

        urllib.request does not reliably override Host, so use http.client
        with skip_host=True for full control.
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.putrequest("GET", f"/agent/api/v1/review-meetings{path}", skip_host=True)
        conn.putheader("Host", host_header)
        conn.putheader("Authorization", "Bearer admin-secret")
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, json.loads(body) if body else {}

    def test_review_host_allowlist_rejects_foreign_host(self):
        """Requests arriving via the legacy mail/agent entry (which overwrites
        Authorization with the admin token) must be refused with 421."""
        status, body = self._raw_get_with_host("mail.maifeipin.com")
        self.assertEqual(status, 421)
        self.assertIn("not allowed", body.get("error", "").lower())

    def test_review_host_allowlist_accepts_configured_domain(self):
        """The configured review_meeting_base_url host (edge entry) is accepted."""
        self.server.api_server.config["review_meeting_base_url"] = "https://edge.maifeipin.com"
        _, created = self.create()
        status, _ = self._raw_get_with_host("edge.maifeipin.com", f"/{created['id']}/invite")
        self.assertEqual(status, 200)

    def test_review_host_allowlist_accepts_localhost(self):
        """Server-local calls (127.0.0.1 / localhost, with or without port) pass."""
        _, created = self.create()
        path = f"/{created['id']}/invite"
        for host in ("localhost", f"localhost:{self.server.server_port}",
                     "127.0.0.1", f"127.0.0.1:{self.server.server_port}"):
            status, _ = self._raw_get_with_host(host, path)
            self.assertEqual(status, 200, f"host {host!r} should be allowed")


if __name__ == "__main__":
    unittest.main()

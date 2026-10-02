import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from channels.api import ApiHandler
from channels import api as api_module
from core import review_meeting_api as access
from scripts import review_meeting as room


class ReviewMeetingAPITests(unittest.TestCase):
    def setUp(self):
        with api_module._REVIEW_AUTH_LOCK:
            api_module._REVIEW_AUTH_ATTEMPTS.clear()
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

    def request(self, method, path="", body=None, token=None, timeout=5):
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def create(self):
        return self.request("POST", body={
            "title": "Code review", "brief": "Review local diff and discuss.",
            "owner_key_hash": room.sha("human-only-secret")}, token="admin-secret")

    def join(self, mid, invite, label):
        status, joined = self.request("POST", f"/{mid}/join",
                                      {"invite_token": invite, "label": label})
        self.assertEqual(status, 201)
        return joined["session_token"]

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
        self.assertIn("驻留轮询", first["protocol"])
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
        self.assertEqual(self.request("GET", f"/{mid}", token=qwen_token)[0], 200)
        key = Path(self.temp.name) / "owner.key"
        key.write_text("human-only-secret")
        key.chmod(0o600)
        with closing(room.connect(self.db_path)) as db:
            with db:
                room.execute(db, SimpleNamespace(action="decide", id=mid, decision="approve",
                    note_file=str(summary), owner_key_file=str(key), owner_token_stdin=False,
                    confirm="approve:" + mid))
        status, approved = self.request("GET", f"/{mid}", token=qwen_token)
        self.assertEqual(status, 200)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(self.request("POST", f"/{mid}/leave", token=qwen_token)[0], 200)
        with closing(room.connect(self.db_path)) as db:
            self.assertTrue(room.check_chain(room.events(db, mid), mid)["valid"])

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

    def test_list_meetings_admin_only(self):
        self.assertEqual(self.request("GET", token="guest-secret")[0], 403)
        _, first = self.create()
        _, second = self.create()
        status, body = self.request("GET", token="admin-secret")
        self.assertEqual(status, 200)
        meetings = body["meetings"]
        self.assertEqual({m["id"] for m in meetings}, {first["id"], second["id"]})
        for m in meetings:
            self.assertIn(m["state"], ("open", "waiting"))
            self.assertNotIn("owner_key_hash", m)
            self.assertNotIn("brief", m)

    def test_admin_create_without_owner_key_autoprovisions(self):
        key_path = self.db_path.parent / "owner.key"
        status, first = self.request("POST", body={
            "title": "Dashboard meeting", "brief": "from todo"}, token="admin-secret")
        self.assertEqual(status, 201)
        self.assertTrue(key_path.exists())
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
        status, second = self.request("POST", body={
            "title": "Another", "brief": "reuse key"}, token="admin-secret")
        self.assertEqual(status, 201)
        expected = room.sha(key_path.read_text(encoding="utf-8").strip())
        with closing(room.connect(self.db_path)) as db:
            hashes = {r["owner_key_hash"] for r in db.execute(
                "SELECT owner_key_hash FROM meetings WHERE id IN (?,?)",
                (first["id"], second["id"]))}
        self.assertEqual(hashes, {expected})

    def test_admin_can_read_detail_without_seat(self):
        _, created = self.create()
        mid = created["id"]
        invite = created["invite_url"].split("#invite=", 1)[1]
        _, joined = self.request("POST", f"/{mid}/join", {"invite_token": invite, "label": "codex"})
        self.request("POST", f"/{mid}/reviews", {
            "position": "support", "text": "looks good"}, token=joined["session_token"])
        status, snap = self.request("GET", f"/{mid}", token="admin-secret")
        self.assertEqual(status, 200)
        self.assertEqual(snap["id"], mid)
        self.assertTrue(any(e["kind"] == "review" for e in snap["events"]))
        self.assertEqual(self.request("GET", f"/{mid}", token="guest-secret")[0], 401)
        self.assertEqual(self.request("GET", f"/{mid}?since=-1", token="admin-secret")[0], 400)

    def test_host_comment_as_admin_written_to_audit_chain(self):
        _, created = self.create()
        mid = created["id"]
        status, result = self.request("POST", f"/{mid}/comments", {
            "text": "主持人提示：请聚焦收益与风险"}, token="admin-secret")
        self.assertEqual(status, 201)
        self.assertEqual(result["actor"], "human")
        status, snap = self.request("GET", f"/{mid}", token="admin-secret")
        self.assertEqual(status, 200)
        host_comments = [e for e in snap["events"]
                         if e["kind"] == "comment" and e["actor"] == "human"]
        self.assertEqual(len(host_comments), 1)
        self.assertEqual(host_comments[0]["body"]["text"], "主持人提示：请聚焦收益与风险")
        with closing(room.connect(self.db_path)) as db:
            self.assertTrue(room.check_chain(room.events(db, mid), mid)["valid"])

    def test_host_comment_rejects_empty_text(self):
        _, created = self.create()
        mid = created["id"]
        status, body = self.request("POST", f"/{mid}/comments", {
            "text": "   "}, token="admin-secret")
        self.assertEqual(status, 400)
        self.assertIn("空", body["error"])

    def test_host_comment_rejected_when_meeting_not_open(self):
        _, created = self.create()
        mid = created["id"]
        with closing(room.connect(self.db_path)) as db:
            with db:
                db.execute("UPDATE meetings SET state='approved' WHERE id=?", (mid,))
        status, body = self.request("POST", f"/{mid}/comments", {
            "text": "迟到的主持发言"}, token="admin-secret")
        self.assertEqual(status, 400)
        self.assertIn("开放讨论", body["error"])

    # ---- P0 长轮询 ----
    def _poller(self, path, token, out, timeout=15):
        out.append(self.request("GET", path, token=token, timeout=timeout))

    def _max_seq(self, mid, token="admin-secret"):
        _, snap = self.request("GET", f"/{mid}", token=token)
        return max((e["seq"] for e in snap["events"]), default=0)

    def test_long_poll_wakes_on_new_event(self):
        _, created = self.create()
        mid = created["id"]
        since = self._max_seq(mid)
        out = []
        t = threading.Thread(target=self._poller,
                             args=(f"/{mid}?since={since}&wait=10", "admin-secret", out), daemon=True)
        t.start()
        time.sleep(0.5)
        started = time.monotonic()
        self.assertEqual(self.request("POST", f"/{mid}/comments", {
            "text": "唤醒事件"}, token="admin-secret")[0], 201)
        t.join(timeout=15)
        self.assertLess(time.monotonic() - started, 9)
        self.assertEqual(len(out), 1)
        status, snap = out[0]
        self.assertEqual(status, 200)
        self.assertTrue(any(e["kind"] == "comment" and e["body"]["text"] == "唤醒事件"
                            for e in snap["events"]))

    def test_long_poll_timeout_returns_empty(self):
        _, created = self.create()
        mid = created["id"]
        since = self._max_seq(mid)
        started = time.monotonic()
        status, snap = self.request("GET", f"/{mid}?since={since}&wait=1",
                                    token="admin-secret", timeout=15)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(time.monotonic() - started, 0.8)
        self.assertEqual(snap["events"], [])

    def test_long_poll_immediate_when_events_exist(self):
        _, created = self.create()
        mid = created["id"]
        started = time.monotonic()
        status, snap = self.request("GET", f"/{mid}?since=0&wait=10",
                                    token="admin-secret", timeout=15)
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(snap["events"])

    def test_long_poll_immediate_when_final(self):
        _, created = self.create()
        mid = created["id"]
        since = self._max_seq(mid)
        with closing(room.connect(self.db_path)) as db:
            with db:
                db.execute("UPDATE meetings SET state='approved' WHERE id=?", (mid,))
        started = time.monotonic()
        status, snap = self.request("GET", f"/{mid}?since={since}&wait=10",
                                    token="admin-secret", timeout=15)
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(snap["state"], "approved")

    def test_single_outstanding_poll_per_seat(self):
        _, created = self.create()
        mid = created["id"]
        invite = created["invite_url"].split("#invite=", 1)[1]
        session = self.join(mid, invite, "codex")
        since = self._max_seq(mid)
        out = []
        t = threading.Thread(target=self._poller,
                             args=(f"/{mid}?since={since}&wait=5", session, out), daemon=True)
        t.start()
        time.sleep(0.5)
        status, body = self.request("GET", f"/{mid}?since={since}&wait=5", token=session)
        self.assertEqual(status, 409)
        self.assertIn("挂起", body["error"])
        self.assertEqual(self.request("GET", f"/{mid}?since={since}&wait=0",
                                      token=session)[0], 200)  # wait=0 不受限
        t.join(timeout=15)
        self.assertEqual(out[0][0], 200)
        # 挂起结束后席位可再次长轮询
        self.assertEqual(self.request("GET", f"/{mid}?since={since}&wait=0", token=session)[0], 200)

    def test_wait_param_validation(self):
        _, created = self.create()
        mid = created["id"]
        self.assertEqual(self.request("GET", f"/{mid}?wait=abc", token="admin-secret")[0], 400)
        self.assertEqual(self.request("GET", f"/{mid}?wait=-1", token="admin-secret")[0], 400)

    # ---- P1 hint + 派生在线状态 ----
    def test_hint_and_online_presence(self):
        _, created = self.create()
        mid = created["id"]
        invite = created["invite_url"].split("#invite=", 1)[1]
        session = self.join(mid, invite, "codex")
        status, snap = self.request("GET", f"/{mid}", token=session)
        self.assertEqual(status, 200)
        codex = next(p for p in snap["participants"] if p["name"] == "codex")
        self.assertTrue(codex["online"])
        self.assertIsNotNone(codex["last_poll_at"])
        self.assertIn("提交本轮正式意见", snap["hint"])
        self.assertEqual(self.request("POST", f"/{mid}/reviews", {
            "position": "support", "text": "looks good"}, token=session)[0], 201)
        _, snap = self.request("GET", f"/{mid}", token=session)
        self.assertIn("已提交", snap["hint"])
        _, snap = self.request("GET", f"/{mid}", token="admin-secret")
        self.assertIn("讨论进行中", snap["hint"])

    # ---- P2 advance / waive ----
    def test_advance_waive_admin_only(self):
        _, created = self.create()
        mid = created["id"]
        invite = created["invite_url"].split("#invite=", 1)[1]
        codex = self.join(mid, invite, "codex")
        self.join(mid, invite, "trae")
        self.assertEqual(self.request("POST", f"/{mid}/advance", token=codex)[0], 403)
        self.assertEqual(self.request("POST", f"/{mid}/waive", {
            "names": "trae", "reason": "无响应"}, token=codex)[0], 403)
        self.assertEqual(self.request("POST", f"/{mid}/reviews", {
            "position": "support", "text": "ok"}, token=codex)[0], 201)
        # trae 未交且未注明缺席：不可推进
        self.assertEqual(self.request("POST", f"/{mid}/advance", token="admin-secret")[0], 400)
        status, _ = self.request("POST", f"/{mid}/waive", {
            "names": "trae", "reason": "无响应"}, token="admin-secret")
        self.assertEqual(status, 200)
        self.assertEqual(self.request("POST", f"/{mid}/waive", {
            "names": "nobody", "reason": "x"}, token="admin-secret")[0], 400)
        status, snap = self.request("POST", f"/{mid}/advance", token="admin-secret")
        self.assertEqual(status, 200)
        self.assertEqual(snap["round"], 2)
        _, snap = self.request("GET", f"/{mid}", token="admin-secret")
        self.assertEqual(snap["round"], 2)
        with closing(room.connect(self.db_path)) as db:
            self.assertTrue(room.check_chain(room.events(db, mid), mid)["valid"])

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
        """Hosts outside the allowlist (base_url host + configured extras +
        localhost) must be refused with 421."""
        status, body = self._raw_get_with_host("evil.example.com")
        self.assertEqual(status, 421)
        self.assertIn("not allowed", body.get("error", "").lower())

    def test_review_host_allowlist_accepts_configured_extra_hosts(self):
        """review_meeting_allowed_hosts extends the allowlist (e.g. the
        dashboard origin mail.maifeipin.com); unlisted hosts stay 421."""
        self.server.api_server.config["review_meeting_allowed_hosts"] = ["mail.maifeipin.com"]
        _, created = self.create()
        path = f"/{created['id']}/invite"
        status, _ = self._raw_get_with_host("mail.maifeipin.com", path)
        self.assertEqual(status, 200)
        status, _ = self._raw_get_with_host("other.example.com", path)
        self.assertEqual(status, 421)

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

    def _auth_request(self, body, origin="https://mail.maifeipin.com"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request("POST", "/agent/api/v1/auth", json.dumps(body),
                         {"Content-Type": "application/json", "Origin": origin})
            response = conn.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            conn.close()

    def _host_token(self):
        with closing(room.connect(self.db_path)) as db:
            access.ensure_tables(db)
            with db:
                return access.issue_host_session(db, "host")["review_token"]

    def test_login_issues_only_scoped_hashed_session(self):
        passwd = Path(self.temp.name) / "htpasswd"
        passwd.write_text("host:" + "{SHA}" + __import__("base64").b64encode(
            __import__("hashlib").sha1(b"correct password").digest()).decode() + "\n")
        self.server.api_server.config["dashboard_htpasswd_path"] = str(passwd)
        status, body, headers = self._auth_request({"username": "host", "password": "correct password"})
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], "review-meetings:host")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://mail.maifeipin.com")
        token = body["review_token"]
        self.assertNotIn("admin-secret", json.dumps(body))
        self.assertAlmostEqual(body["review_token_expires_at"], time.time() + 3600, delta=2)
        with closing(room.connect(self.db_path)) as db:
            row = db.execute("SELECT * FROM review_host_sessions").fetchone()
            self.assertEqual(row["token_hash"], room.sha(token))
            self.assertNotEqual(row["token_hash"], token)
        self.assertEqual(self.request("GET", token=token)[0], 200)
        self.assertEqual(row["role"], "host")
        with patch.object(access, "issue_host_session", side_effect=__import__("sqlite3").OperationalError("storage failed")):
            failed_status, failed_body, _ = self._auth_request({"username": "host", "password": "correct password"})
        self.assertEqual(failed_status, 503)
        self.assertNotIn("review_token", failed_body)
        self.assertEqual(self._auth_request({"username": "host", "password": "wrong"})[0], 401)
        self.assertEqual(self._auth_request(["not", "an", "object"])[0], 400)
        self.assertEqual(self._auth_request({"username": 9, "password": "wrong"})[0], 401)
        _, _, denied = self._auth_request({"username": "host", "password": "wrong"}, "https://evil.example")
        self.assertNotIn("Access-Control-Allow-Origin", denied)

    def test_host_session_management_scope_expiry_and_revocation(self):
        token = self._host_token()
        status, created = self.request("POST", body={"title": "Scoped", "brief": "Scoped host"}, token=token)
        self.assertEqual(status, 201)
        mid = created["id"]
        seat = self.join(mid, created["invite_url"].split("#invite=")[1], "codex")
        self.assertEqual(self.request("GET", token=seat)[0], 403)
        self.assertEqual(self.request("POST", f"/{mid}/advance", token=seat)[0], 403)
        self.assertEqual(self.request("POST", f"/{mid}/waive", {"names": ["codex"], "reason": "scope test"}, token=token)[0], 200)
        self.assertEqual(self.request("POST", f"/{mid}/advance", token=token)[0], 200)
        self.assertEqual(self.request("POST", f"/{mid}/comments", {"text": "Host comment"}, token=token)[0], 201)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            conn.request("GET", "/agent/api/v1/sessions", headers={"Authorization": "Bearer " + token})
            r = conn.getresponse()
            self.assertEqual(r.status, 403)
            r.read()
        finally:
            conn.close()
        self.assertEqual(self.request("POST", f"/{mid}/decide", {"decision": "approve"}, token=token)[0], 404)
        self.assertEqual(self.request("POST", f"/{mid}/invites", {}, token=token)[0], 201)
        self.assertEqual(self.request("POST", "/logout", {}, token=token)[0], 200)
        self.assertEqual(self.request("GET", token=token)[0], 403)
        self.assertEqual(self.request("POST", "/logout", {}, token=token)[0], 401)
        other = self._host_token()
        with closing(room.connect(self.db_path)) as db:
            with db:
                db.execute("UPDATE review_host_sessions SET expires_at=? WHERE token_hash=?", (int(time.time()), room.sha(other)))
        self.assertEqual(self.request("GET", token=other)[0], 403)
        self.assertEqual(self.request("POST", "/logout", {}, token=seat)[0], 401)
        self.assertEqual(self.request("GET", f"/{mid}", token=seat)[0], 401)

    def test_login_rate_limit_counts_invalid_attempts(self):
        for _ in range(10):
            self.assertEqual(self._auth_request({"username": "", "password": ""})[0], 401)
        self.assertEqual(self._auth_request({"username": "", "password": ""})[0], 429)

    def test_seat_reads_frozen_final_and_archived_states_but_cannot_write(self):
        _, created = self.create()
        mid = created["id"]
        token = self.join(mid, created["invite_url"].split("#invite=")[1], "codex")
        for state in ("awaiting_approval", "changes_requested", "approved", "rejected"):
            with closing(room.connect(self.db_path)) as db:
                with db:
                    db.execute("UPDATE meetings SET state=? WHERE id=?", (state, mid))
            status, snapshot = self.request("GET", f"/{mid}", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(snapshot["state"], state)
            self.assertTrue(snapshot["hint"])
            self.assertEqual(self.request("POST", f"/{mid}/comments", {"text": "Cannot write"}, token=token)[0], 403)
            self.assertEqual(self.request("POST", f"/{mid}/reviews", {"position": "support", "text": "Cannot write"}, token=token)[0], 403)
        with closing(room.connect(self.db_path)) as db:
            with db:
                db.execute("UPDATE meetings SET archived_at=? WHERE id=?", (int(time.time()), mid))
        self.assertEqual(self.request("GET", f"/{mid}", token=token)[0], 200)
        self.assertEqual(self.request("POST", f"/{mid}/leave", token=token)[0], 200)
        self.assertEqual(self.request("GET", f"/{mid}", token=token)[0], 401)

    def test_seat_longpoll_observes_terminal_transition_and_releases_slot(self):
        _, created = self.create()
        mid = created["id"]
        token = self.join(mid, created["invite_url"].split("#invite=")[1], "codex")
        _, snapshot = self.request("GET", f"/{mid}", token=token)
        seq = max(e["seq"] for e in snapshot["events"])
        result = []
        worker = threading.Thread(target=lambda: result.append(self.request(
            "GET", f"/{mid}?since={seq}&wait=2", token=token)))
        worker.start()
        time.sleep(0.2)
        with closing(room.connect(self.db_path)) as db:
            with db:
                db.execute("UPDATE meetings SET state='approved' WHERE id=?", (mid,))
        worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][0], 200)
        self.assertEqual(result[0][1]["state"], "approved")
        self.assertNotIn((mid, "codex"), api_module._REVIEW_WAITERS)
        self.assertEqual(self.request("GET", f"/{mid}?since={seq}&wait=1", token=token)[0], 200)

    def test_waived_seat_is_not_pending_in_hint(self):
        _, created = self.create()
        mid = created["id"]
        invite = created["invite_url"].split("#invite=")[1]
        token = self.join(mid, invite, "codex")
        self.join(mid, invite, "qwen")
        self.assertEqual(self.request("POST", f"/{mid}/reviews", {"position": "revise", "text": "Ready"}, token=token)[0], 201)
        self.assertEqual(self.request("POST", f"/{mid}/waive", {"names": ["qwen"], "reason": "Absent"}, token="admin-secret")[0], 200)
        _, snapshot = self.request("GET", f"/{mid}", token=token)
        self.assertEqual(snapshot["missing"], [])
        self.assertNotIn("qwen", snapshot["hint"])
        self.assertIn("全员已齐", snapshot["hint"])

    def test_non_finite_wait_is_invalid(self):
        _, created = self.create()
        for value in ("nan", "inf", "-inf"):
            self.assertEqual(self.request("GET", f"/{created['id']}?wait={value}", token="admin-secret")[0], 400)

    def test_host_longpoll_rechecks_revoked_session_before_response(self):
        token = self._host_token()
        _, created = self.create()
        mid = created["id"]
        _, snapshot = self.request("GET", f"/{mid}", token=token)
        seq = max(e["seq"] for e in snapshot["events"])
        results = []
        worker = threading.Thread(target=lambda: results.append(self.request(
            "GET", f"/{mid}?since={seq}&wait=1", token=token)))
        worker.start()
        time.sleep(0.2)
        self.assertEqual(self.request("POST", "/logout", {}, token=token)[0], 200)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0][0], 401)

    def test_meeting_cors_preflight_allows_dashboard_not_other_origins(self):
        for origin, allowed in (("https://mail.maifeipin.com", True), ("https://evil.example", False)):
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
            try:
                conn.request("OPTIONS", "/agent/api/v1/review-meetings", headers={
                    "Origin": origin, "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization,Content-Type"})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Access-Control-Allow-Origin"), origin if allowed else None)
                self.assertIn("Authorization", response.getheader("Access-Control-Allow-Headers"))
                response.read()
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

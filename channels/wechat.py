"""WeChat personal-message channel backed by the iLink protocol client."""

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from agent import AgentResponse, IncomingMessage
from channels.base import BaseChannel
from channels.wechat_ilink import (
    ILinkAuthError,
    ILinkClient,
    ILinkContextError,
    ILinkNetworkError,
)
from core.constants import PROJECT_ROOT


MAX_PROTOCOL_TEXT_BYTES = 16384
MAX_SEGMENTS_PER_RESPONSE = 5


def _project_path(value: str) -> str:
    return value if os.path.isabs(value) else os.path.join(PROJECT_ROOT, value)


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    """Persist channel state with the same 0600 + replace guarantee as session data."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(
        dir=directory, prefix=".wechat_context_", suffix=".tmp", text=True
    )
    try:
        os.chmod(temp_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


class WeChatChannel(BaseChannel):
    """Personal WeChat channel with per-user iLink context-token state."""

    def __init__(self, config: dict, agent) -> None:
        super().__init__("wechat", config, agent)
        self.admin_wxid = str(config.get("admin_wxid", "") or "")
        self.max_msg_len = max(1, min(int(config.get("max_msg_len", 2000)), MAX_PROTOCOL_TEXT_BYTES))
        self.context_ttl = max(1.0, float(config.get("context_ttl_hours", 24)) * 3600)
        self.context_max_sends = max(1, int(config.get("context_max_sends", 10)))
        self.contexts_file = _project_path(config.get("contexts_file", "data/wechat_contexts.json"))
        self.client = ILinkClient(
            session_file=config.get("session_file", "data/wechat_session.json"),
            poll_timeout=int(config.get("poll_timeout", 35)),
            cursor_file=config.get("cursor_file", "data/wechat_cursor.json"),
        )
        self._contexts: Dict[str, Dict[str, Any]] = self._load_contexts()
        self._context_lock = threading.RLock()
        self._running = False
        self._online = False
        self._offline_reported = False
        self._stale_probe_pending = False
        self._isolate_until = 0.0
        self._warned_no_admin = False
        self._notifier = None
        self.executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="WeChatWorker")

    def set_admin_notifier(self, fn) -> None:
        """Set the cross-channel, best-effort offline status notifier."""
        self._notifier = fn
        if self._running and not self._online:
            self._offline_reported = False
            self._set_offline("微信通道离线，等待凭据或网络恢复")

    def start(self) -> None:
        """Start a worker only; service mode never invokes the QR-login flow."""
        if self._running:
            return
        self._running = True
        self.executor.submit(self._poll_loop)

    def stop(self) -> None:
        self._running = False
        try:
            self.client.close()
        except Exception:
            pass
        self.executor.shutdown(wait=False)

    def send_response(self, message_id: str, response: AgentResponse) -> bool:
        talker = self._parse_talker(message_id)
        if not talker:
            print("  [WeChat] cannot route a response without a talker")
            return False
        token = self._valid_token(talker)
        if not token:
            return False
        return self._send_segmented(talker, token, self._response_text(response))

    def send_to(self, open_id: str, response: AgentResponse) -> bool:
        token = self._valid_token(open_id)
        if not open_id or not token:
            return False
        return self._send_segmented(open_id, token, self._response_text(response))

    def send_progress(self, message_id: str, text: str = "") -> bool:
        talker = self._parse_talker(message_id)
        token = self._valid_token(talker) if talker else None
        if not talker or not token:
            return False
        return self._send_segmented(talker, token, text or "已收到，AI 正在分析中…")

    def push_result(self, msg, response: AgentResponse) -> bool:
        talker = (msg.channel_payload or {}).get("talker")
        token = (msg.channel_payload or {}).get("context_token")
        if not talker:
            talker = self._parse_talker(msg.message_id)
        if not talker:
            return False
        return self._send_segmented(talker, token or self._valid_token(talker), self._response_text(response))

    def push_progress(self, msg, text: str) -> bool:
        talker = (msg.channel_payload or {}).get("talker")
        token = (msg.channel_payload or {}).get("context_token")
        if not talker:
            talker = self._parse_talker(msg.message_id)
        if not talker:
            return False
        return self._send_segmented(talker, token or self._valid_token(talker), text or "处理中…")

    def broadcast(self, response: AgentResponse) -> bool:
        users: List[str] = []
        try:
            with self.agent.session_mgr._connect() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT session_key FROM sessions WHERE session_key LIKE 'wechat:%'"
                ).fetchall()
            users = [row[0].split(":", 1)[1] for row in rows if ":" in row[0]]
        except Exception as exc:
            print("  [WeChat] could not query broadcast recipients: %s" % exc)
            return False

        sent = 0
        for user_id in users:
            if self.send_to(user_id, response):
                sent += 1
        print("  [WeChat] broadcast delivered to %s/%s eligible users" % (sent, len(users)))
        return sent > 0

    def _poll_loop(self) -> None:
        backoff = 1
        while self._running:
            if not self.client.load_credential():
                self._set_offline("微信凭据缺失或无效；请通过登录 CLI 更新凭据")
                self._wait(60)
                continue

            if self._isolate_until > time.time():
                self._set_offline("微信凭据处于协议隔离观察期")
                self._wait(min(60, self._isolate_until - time.time()))
                continue

            try:
                updates = self.client.get_updates()
            except ILinkNetworkError:
                self._set_offline("微信网络暂时不可用")
                self._wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            except ILinkAuthError as exc:
                self._handle_auth_error(exc)
                continue

            backoff = 1
            self._stale_probe_pending = False
            self._set_online()
            for raw in updates:
                if self._running:
                    self._handle_update(raw)

    def _handle_auth_error(self, error: ILinkAuthError, wait: bool = True) -> None:
        # get_updates(-14) 已由客户端清理游标；sendmessage(-14) 也必须走同一 C5 语义。
        try:
            self.client.clear_cursor()
        except Exception:
            pass
        self._clear_contexts()
        if error.permanent:
            self.client.logout_cleanup()
            self._stale_probe_pending = False
            self._isolate_until = 0.0
            self._set_offline("微信凭据已失效；请通过登录 CLI 更新凭据")
            if wait:
                self._wait(60)
            return

        if self._stale_probe_pending:
            self.client.logout_cleanup()
            self._stale_probe_pending = False
            self._isolate_until = 0.0
            self._set_offline("微信凭据连续返回 -14；请通过登录 CLI 更新凭据")
            if wait:
                self._wait(60)
            return

        self._stale_probe_pending = True
        self._isolate_until = time.time() + 3600
        self._set_offline("微信凭据返回 -14，已隔离一小时后进行一次探测")

    def _handle_update(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        if raw.get("message_type") != 1 or raw.get("message_state") != 2:
            return
        talker = raw.get("from_user_id")
        if not isinstance(talker, str) or not talker:
            return
        token = raw.get("context_token")
        if isinstance(token, str) and token:
            self._remember_context(talker, token)
        if raw.get("group_id"):
            print("  [WeChat] group message ignored (Phase 1 direct-message only)")
            return

        text = self._extract_text(raw)
        message_id = self._message_id(talker, raw)
        if self.agent.session_mgr.is_message_processed(message_id):
            return
        if not text:
            valid_token = self._valid_token(talker)
            if valid_token:
                self._send_segmented(talker, valid_token, "当前仅支持文本消息。")
            return

        is_guest = talker != self.admin_wxid if self.admin_wxid else True
        if not self.admin_wxid and not self._warned_no_admin:
            self._warned_no_admin = True
            print("  [WeChat] admin_wxid is not configured; all users are treated as guests")
        incoming = IncomingMessage(
            channel="wechat",
            user_id=talker,
            chat_id=talker,
            message_id=message_id,
            text=text,
            is_guest=is_guest,
            channel_payload={"talker": talker, "context_token": token or ""},
        )
        self.executor.submit(self._process_and_reply, incoming)

    def _process_and_reply(self, message: IncomingMessage) -> None:
        try:
            from channels import smart_truncate
            self.send_progress(message.message_id, "已收到 \"%s\"" % smart_truncate(message.text, 50))
            response = self.agent.handle(message)
            if response:
                self.send_response(message.message_id, response)
        except Exception as exc:
            print("  [WeChat] agent processing failed: %s" % exc)

    @staticmethod
    def _extract_text(raw: Dict[str, Any]) -> str:
        parts = raw.get("item_list")
        if not isinstance(parts, list):
            return ""
        chunks: List[str] = []
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != 1:
                continue
            text_item = part.get("text_item")
            if isinstance(text_item, dict) and isinstance(text_item.get("text"), str):
                chunks.append(text_item["text"])
        return "".join(chunks).strip()

    @staticmethod
    def _message_id(talker: str, raw: Dict[str, Any]) -> str:
        server_id = raw.get("message_id")
        candidate = str(server_id) if server_id is not None else ""
        if re.match(r"^[A-Za-z0-9-]+$", candidate):
            suffix = candidate
        else:
            canonical = json.dumps(raw.get("item_list", []), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fingerprint = "%s|%s|%s|%s|%s" % (
                talker,
                raw.get("to_user_id", ""),
                raw.get("session_id", ""),
                raw.get("create_time_ms", ""),
                canonical,
            )
            suffix = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        return "wechat_%s_%s" % (talker, suffix)

    @staticmethod
    def _parse_talker(message_id: str) -> Optional[str]:
        if not isinstance(message_id, str) or not message_id.startswith("wechat_"):
            return None
        talker, separator, _suffix = message_id[len("wechat_"):].rpartition("_")
        return talker if separator and talker else None

    @staticmethod
    def _response_text(response: AgentResponse) -> str:
        return "**%s**\n\n%s" % (response.title, response.text) if response.title else response.text

    def _send_segmented(self, talker: str, token: Optional[str], text: str) -> bool:
        if not talker or not token:
            return False
        current = self._valid_token(talker)
        if not current:
            return False
        segments = self._split_text(text)
        if not segments:
            return True
        with self._context_lock:
            state = self._contexts.get(talker, {})
            remaining = self.context_max_sends - int(state.get("sends_used", 0))
        if remaining <= 0:
            return False
        segments = segments[:remaining]

        delivered = False
        for segment in segments:
            try:
                if not self.client.send_text(talker, token, segment):
                    return delivered
            except ILinkContextError:
                self._invalidate_context(talker, token)
                return False
            except ILinkAuthError as exc:
                self._handle_auth_error(exc, wait=False)
                return False
            except ILinkNetworkError:
                return False
            self._consume_context(talker, token)
            delivered = True
        return delivered

    def _split_text(self, text: str) -> List[str]:
        if not text:
            return []
        segments: List[str] = []
        current: List[str] = []
        current_bytes = 0
        for character in text:
            size = len(character.encode("utf-8"))
            if current and current_bytes + size > self.max_msg_len:
                segments.append("".join(current))
                if len(segments) >= MAX_SEGMENTS_PER_RESPONSE:
                    return segments
                current = []
                current_bytes = 0
            current.append(character)
            current_bytes += size
        if current and len(segments) < MAX_SEGMENTS_PER_RESPONSE:
            segments.append("".join(current))
        return segments

    def _valid_token(self, talker: Optional[str]) -> Optional[str]:
        if not talker:
            return None
        with self._context_lock:
            state = self._contexts.get(talker)
            if not state:
                return None
            if time.time() - float(state.get("updated_at", 0)) >= self.context_ttl:
                return None
            if int(state.get("sends_used", 0)) >= self.context_max_sends:
                return None
            token = state.get("context_token")
            return token if isinstance(token, str) and token else None

    def _remember_context(self, talker: str, token: str) -> None:
        with self._context_lock:
            previous = self._contexts.get(talker, {})
            used = int(previous.get("sends_used", 0)) if previous.get("context_token") == token else 0
            self._contexts[talker] = {
                "context_token": token,
                "updated_at": time.time(),
                "sends_used": used,
            }
            self._save_contexts_locked()

    def _consume_context(self, talker: str, token: Optional[str] = None) -> None:
        with self._context_lock:
            state = self._contexts.get(talker)
            if not state:
                return
            if token and state.get("context_token") != token:
                return
            state["sends_used"] = int(state.get("sends_used", 0)) + 1
            self._save_contexts_locked()

    def _invalidate_context(self, talker: str, token: Optional[str] = None) -> None:
        with self._context_lock:
            state = self._contexts.get(talker)
            if state:
                if token and state.get("context_token") != token:
                    return
                state["sends_used"] = self.context_max_sends
                self._save_contexts_locked()

    def _load_contexts(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.contexts_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        clean: Dict[str, Dict[str, Any]] = {}
        for user_id, state in payload.items():
            if not isinstance(user_id, str) or not isinstance(state, dict):
                continue
            token = state.get("context_token")
            updated_at = state.get("updated_at")
            sends_used = state.get("sends_used", 0)
            if isinstance(token, str) and token and isinstance(updated_at, (int, float)) and isinstance(sends_used, int):
                clean[user_id] = {
                    "context_token": token,
                    "updated_at": float(updated_at),
                    "sends_used": max(0, sends_used),
                }
        return clean

    def _clear_contexts(self) -> None:
        with self._context_lock:
            self._contexts = {}
            try:
                os.remove(self.contexts_file)
            except FileNotFoundError:
                pass
            except OSError:
                print("  [WeChat] could not clear context store")

    def _save_contexts_locked(self) -> None:
        payload = {
            user_id: {
                "context_token": state["context_token"],
                "updated_at": state["updated_at"],
                "sends_used": state["sends_used"],
            }
            for user_id, state in self._contexts.items()
        }
        _atomic_write_json(self.contexts_file, payload)

    def _set_online(self) -> None:
        if not self._online:
            print("  [WeChat] iLink channel is online")
        self._online = True
        self._offline_reported = False

    def _set_offline(self, reason: str) -> None:
        self._online = False
        if self._offline_reported:
            return
        print("  [WeChat] %s" % reason)
        if self._notifier:
            try:
                self._notifier(reason, "微信通道")
            except Exception:
                pass
        self._offline_reported = True

    def _wait(self, seconds: float) -> None:
        deadline = time.time() + max(0, seconds)
        while self._running and time.time() < deadline:
            time.sleep(min(1, max(0.0, deadline - time.time())))

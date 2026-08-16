"""Minimal client for the official WeChat iLink Bot HTTP protocol.

This module deliberately has no dependency on the agent or channel layers.  It
owns only the wire protocol, the login credential, and the opaque getupdates
cursor.  Context-token lifetime and outbound quotas belong to WeChatChannel
(Phase 1), where they can be tracked per user.
"""

import argparse
import base64
import json
import os
import random
import tempfile
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import httpx

from core.constants import PROJECT_ROOT


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.6"
APP_CLIENT_VERSION = "132102"
DEFAULT_CURSOR_FILE = "data/wechat_cursor.json"


class ILinkAuthError(Exception):
    """An iLink credential is not usable.

    ``permanent`` is False for protocol code -14, which is an hour-long
    server-side isolation signal.  It is True for HTTP 401/403, where local
    credentials must be replaced through the interactive login CLI.
    """

    def __init__(self, message: str, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent


class ILinkNetworkError(Exception):
    """A retryable network, timeout, or server-side failure."""


class ILinkContextError(Exception):
    """The target user's context token is no longer usable (protocol ret=-2)."""


def _project_path(value: str) -> str:
    """Resolve relative runtime files consistently with the rest of the app."""
    return value if os.path.isabs(value) else os.path.join(PROJECT_ROOT, value)


def _protocol_code(payload: Dict[str, Any]) -> int:
    """Return an iLink semantic status code; absent/invalid means success."""
    for name in ("ret", "errcode"):
        value = payload.get(name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1
    return 0


class ILinkClient:
    """Small, testable iLink HTTP client.

    ``http_client`` is intentionally injectable for fixture-driven contract
    tests.  Production callers leave it as ``None`` and receive a normal
    ``httpx.Client`` with a timeout safely above the long-poll window.
    """

    def __init__(
        self,
        session_file: str,
        poll_timeout: int = 35,
        cursor_file: str = DEFAULT_CURSOR_FILE,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.session_file = _project_path(session_file)
        self.cursor_file = _project_path(cursor_file)
        self.poll_timeout = poll_timeout
        self._http = http_client or httpx.Client(timeout=poll_timeout + 10)
        self._credential: Dict[str, str] = {}
        self._cursor = self._load_cursor()

    def close(self) -> None:
        """Close the owned HTTP client when the caller is done with it."""
        self._http.close()

    def load_credential(self) -> bool:
        """Load the credential white-list without exposing its contents."""
        try:
            with open(self.session_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            self._credential = {}
            return False
        except (OSError, ValueError, TypeError):
            self._credential = {}
            print("  [WeChat iLink] session file is invalid; login is required")
            return False

        normalized = self._normalize_credential(payload)
        if normalized is None:
            self._credential = {}
            print("  [WeChat iLink] session file is incomplete; login is required")
            return False
        self._credential = normalized
        return True

    def ensure_login_cli(
        self,
        on_qrcode: Callable[[str], None],
        on_status: Callable[[str], None] = print,
    ) -> None:
        """Interactively acquire and persist a credential; never call in service mode."""
        local_tokens: List[str] = []
        if self.load_credential() and self._credential.get("token"):
            local_tokens.append(self._credential["token"])

        for _attempt in range(3):
            response = self._request_json(
                "POST",
                DEFAULT_BASE_URL + "/ilink/bot/get_bot_qrcode",
                params={"bot_type": "3"},
                payload={"local_token_list": local_tokens[:10]},
                headers={"Content-Type": "application/json"},
            )
            qrcode = response.get("qrcode")
            image_url = response.get("qrcode_img_content")
            if not isinstance(qrcode, str) or not isinstance(image_url, str):
                raise ILinkNetworkError("iLink QR response did not contain a QR code")

            # qrcode_img_content is an HTTPS URL, not image bytes or base64.
            on_qrcode(image_url)
            if self._poll_qrcode(qrcode, on_status):
                return
            on_status("QR code expired; requesting a fresh code")
        raise ILinkNetworkError("iLink QR login timed out")

    def get_updates(self) -> List[Dict[str, Any]]:
        """Long-poll messages, keeping the server cursor opaque and durable."""
        credential = self._require_credential()
        payload = {
            "get_updates_buf": self._cursor,
            "base_info": self._base_info(),
        }
        response = self._request_json(
            "POST",
            self._endpoint(credential, "/ilink/bot/getupdates"),
            payload=payload,
            headers=self._business_headers(credential),
        )
        code = _protocol_code(response)
        if code == -14:
            self.clear_cursor()
            raise ILinkAuthError("iLink getupdates returned -14", permanent=False)
        if code != 0:
            print("  [WeChat iLink] getupdates protocol error code=%s" % code)
            return []

        next_cursor = response.get("get_updates_buf")
        if isinstance(next_cursor, str) and next_cursor:
            self._cursor = next_cursor
            self._save_cursor(next_cursor)
        messages = response.get("msgs", [])
        return messages if isinstance(messages, list) else []

    def send_text(self, target_id: str, context_token: str, text: str) -> bool:
        """Send a complete C7 text request and classify semantic failures."""
        if not target_id:
            raise ValueError("target_id is required")
        if not context_token:
            raise ValueError("context_token is required")
        credential = self._require_credential()
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": target_id,
                "client_id": "lite_agent-wechat-" + str(uuid.uuid4()),
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
                "context_token": context_token,
            },
            "base_info": self._base_info(),
        }
        response = self._request_json(
            "POST",
            self._endpoint(credential, "/ilink/bot/sendmessage"),
            payload=payload,
            headers=self._business_headers(credential),
        )
        code = _protocol_code(response)
        if code == -2:
            raise ILinkContextError("iLink sendmessage returned -2")
        if code == -14:
            raise ILinkAuthError("iLink sendmessage returned -14", permanent=False)
        if code != 0:
            print("  [WeChat iLink] sendmessage protocol error code=%s" % code)
            return False
        return True

    def logout_cleanup(self) -> None:
        """Remove only the locally persisted credential, never context state."""
        self._credential = {}
        try:
            os.remove(self.session_file)
        except FileNotFoundError:
            pass
        except OSError:
            print("  [WeChat iLink] could not remove session file")

    def clear_cursor(self) -> None:
        """Forget an invalid opaque getupdates cursor both in memory and on disk."""
        self._cursor = ""
        try:
            os.remove(self.cursor_file)
        except FileNotFoundError:
            pass
        except OSError:
            print("  [WeChat iLink] could not clear cursor file")

    def _poll_qrcode(self, qrcode: str, on_status: Callable[[str], None]) -> bool:
        deadline = time.monotonic() + 480
        verify_code: Optional[str] = None
        status_base_url = DEFAULT_BASE_URL
        while time.monotonic() < deadline:
            params: Dict[str, str] = {"qrcode": qrcode}
            if verify_code:
                params["verify_code"] = verify_code
            response = self._request_json(
                "GET",
                status_base_url + "/ilink/bot/get_qrcode_status",
                params=params,
                headers={"Content-Type": "application/json"},
            )
            status = response.get("status")
            if status == "confirmed":
                self._save_session(response)
                on_status("iLink login confirmed")
                return True
            if status in ("expired", "verify_code_blocked"):
                return False
            if status == "need_verifycode":
                verify_code = input("WeChat verification code: ").strip()
                continue
            if status == "scaned_but_redirect":
                redirect_host = response.get("redirect_host")
                if isinstance(redirect_host, str) and redirect_host:
                    if redirect_host.startswith(("https://", "http://")):
                        status_base_url = redirect_host.rstrip("/")
                    else:
                        status_base_url = "https://" + redirect_host.rstrip("/")
            if status == "binded_redirect" and self.load_credential():
                on_status("iLink account is already bound")
                return True
            if status not in ("wait", "scaned", "scaned_but_redirect"):
                on_status("iLink QR status: %s" % (status or "unknown"))
            time.sleep(1)
        return False

    def _request_json(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, str]] = None,
        payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Perform one request and decode raw text regardless of its MIME type."""
        try:
            response = self._http.request(
                method, url, params=params, json=payload, headers=headers
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
            raise ILinkNetworkError("iLink request failed") from exc

        if response.status_code in (401, 403):
            raise ILinkAuthError("iLink HTTP authentication failed", permanent=True)
        if response.status_code >= 500:
            raise ILinkNetworkError("iLink server returned %s" % response.status_code)
        if not 200 <= response.status_code < 300:
            print("  [WeChat iLink] HTTP status=%s" % response.status_code)
            return {"ret": -1}
        try:
            decoded = json.loads(response.text)
        except (TypeError, ValueError):
            print("  [WeChat iLink] response was not valid JSON")
            return {"ret": -1}
        return decoded if isinstance(decoded, dict) else {}

    def _business_headers(self, credential: Dict[str, str]) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": "Bearer " + credential["token"],
            "X-WECHAT-UIN": self._new_uin(),
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": APP_CLIENT_VERSION,
        }

    @staticmethod
    def _new_uin() -> str:
        value = str(random.SystemRandom().randint(0, (2 ** 32) - 1)).encode("ascii")
        return base64.b64encode(value).decode("ascii")

    @staticmethod
    def _base_info() -> Dict[str, str]:
        return {
            "channel_version": CHANNEL_VERSION,
            "bot_agent": "lite_agent/dev (python)",
        }

    @staticmethod
    def _endpoint(credential: Dict[str, str], path: str) -> str:
        return credential["base_url"].rstrip("/") + path

    def _require_credential(self) -> Dict[str, str]:
        if not self._credential and not self.load_credential():
            raise ILinkAuthError("iLink credential is not loaded", permanent=True)
        return self._credential

    @staticmethod
    def _normalize_credential(payload: Any) -> Optional[Dict[str, str]]:
        if not isinstance(payload, dict):
            return None
        token = payload.get("token") or payload.get("bot_token")
        bot_id = payload.get("bot_id") or payload.get("ilink_bot_id")
        base_url = payload.get("base_url") or payload.get("baseurl")
        user_id = payload.get("user_id") or payload.get("ilink_user_id")
        if not all(isinstance(value, str) and value for value in (token, bot_id, base_url, user_id)):
            return None
        return {
            "token": token,
            "bot_id": bot_id,
            "base_url": base_url,
            "user_id": user_id,
            "saved_at": str(payload.get("saved_at", "")),
        }

    def _save_session(self, payload: Dict[str, Any]) -> None:
        credential = self._normalize_credential(payload)
        if credential is None:
            raise ValueError("iLink login response did not contain a complete credential")
        credential["saved_at"] = str(int(time.time()))
        self._atomic_write_json(self.session_file, credential)
        self._credential = credential

    def _load_cursor(self) -> str:
        try:
            with open(self.cursor_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return ""
        cursor = payload.get("get_updates_buf") if isinstance(payload, dict) else None
        return cursor if isinstance(cursor, str) else ""

    def _save_cursor(self, cursor: str) -> None:
        self._atomic_write_json(self.cursor_file, {"get_updates_buf": cursor})

    @staticmethod
    def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        descriptor, temp_path = tempfile.mkstemp(
            dir=directory, prefix=".wechat_ilink_", suffix=".tmp", text=True
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


def _main() -> int:
    parser = argparse.ArgumentParser(description="WeChat iLink login helper")
    parser.add_argument("--login", action="store_true", help="run the interactive QR login")
    parser.add_argument("--session-file", default="data/wechat_session.json")
    args = parser.parse_args()
    if not args.login:
        parser.error("--login is required")

    client = ILinkClient(session_file=args.session_file)
    try:
        client.ensure_login_cli(
            on_qrcode=lambda url: print("WeChat QR link: " + url),
        )
    except (ILinkNetworkError, ILinkAuthError, ValueError) as exc:
        print("iLink login failed: " + str(exc))
        return 2
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

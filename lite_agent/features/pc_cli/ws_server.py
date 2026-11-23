"""
PC CLI 独立异步 WebSocket 服务 (features.pc_cli.ws_server).
基于 Python 标准库 asyncio 实现 RFC 6455 WebSocket 协议，零第三方依赖。
提供两个端点：
- /ws/pc-cli/daemon : Win/Mac 本地守护进程接入（Bearer DAEMON_TOKEN 鉴权）
- /ws/pc-cli/client : 手机端 PWA 实时订阅（首帧 auth 或 Bearer CLIENT_TOKEN 鉴权）
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import struct
import threading
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

from lite_agent.features.pc_cli.hub import PcCliHub

logger = logging.getLogger("features.pc_cli.ws_server")

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_FRAME_BYTES = 4 * 1024 * 1024  # 4MB 单帧上限


class _WsConnection:
    """RFC 6455 服务端连接封装。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, loop: asyncio.AbstractEventLoop):
        self.reader = reader
        self.writer = writer
        self.loop = loop
        self._closed = False
        self._write_lock = asyncio.Lock()

    async def send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return
        header = bytearray([0x80 | (opcode & 0x0F)])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        async with self._write_lock:
            if self._closed:
                return
            self.writer.write(bytes(header) + payload)
            await self.writer.drain()

    async def send_json(self, data: Dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        await self.send_frame(0x1, raw)

    def send_json_threadsafe(self, data: Dict[str, Any], wait_timeout: float = 0.0) -> None:
        if self._closed or self.loop.is_closed():
            if wait_timeout > 0:
                raise ConnectionError("WebSocket 连接已关闭")
            return
        fut = asyncio.run_coroutine_threadsafe(self.send_json(data), self.loop)
        if wait_timeout > 0:
            fut.result(timeout=wait_timeout)

    async def recv_text(self) -> Optional[str]:
        """读取完整文本消息（支持分片、自动回复 ping、处理 close）。连接关闭返回 None。"""
        fragments: list[bytes] = []
        while not self._closed:
            try:
                head = await self.reader.readexactly(2)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return None

            b1, b2 = head[0], head[1]
            fin = (b1 & 0x80) != 0
            opcode = b1 & 0x0F
            masked = (b2 & 0x80) != 0
            length = b2 & 0x7F

            if length == 126:
                ext = await self.reader.readexactly(2)
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = await self.reader.readexactly(8)
                length = struct.unpack("!Q", ext)[0]

            if length > _MAX_FRAME_BYTES:
                await self.close(1009, "Message too big")
                return None

            mask_key = await self.reader.readexactly(4) if masked else b""
            data = await self.reader.readexactly(length) if length > 0 else b""
            if masked and data:
                data = bytes(b ^ mask_key[i & 3] for i, b in enumerate(data))

            if opcode == 0x8:  # Close
                await self.close(1000)
                return None
            if opcode == 0x9:  # Ping
                await self.send_frame(0xA, data)
                continue
            if opcode == 0xA:  # Pong
                continue
            if opcode in (0x1, 0x0):  # Text or continuation
                fragments.append(data)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")
        return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        try:
            payload = struct.pack("!H", code) + reason.encode("utf-8")[:120]
            self.writer.write(bytes([0x88, len(payload)]) + payload)
            await self.writer.drain()
        except Exception:
            pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


def _parse_http_request(raw_header: bytes) -> Tuple[str, str, Dict[str, str]]:
    lines = raw_header.decode("iso-8859-1", errors="replace").split("\r\n")
    if not lines or not lines[0]:
        raise ValueError("Empty HTTP request")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        raise ValueError("Malformed HTTP request line")
    method, target = parts[0].upper(), parts[1]
    path = target.split("?", 1)[0]
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return method, path, headers


def _extract_bearer(headers: Dict[str, str]) -> str:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


class PcCliWsServer:
    """运行在后台线程中的异步 WebSocket 服务。"""

    def __init__(
        self,
        hub: PcCliHub,
        *,
        host: str = "127.0.0.1",
        port: int = 8889,
        daemon_token: str = "",
        client_token: str = "",
        client_token_verifier: Optional[Callable[[str], bool]] = None,
    ):
        self.hub = hub
        self.host = host
        self.port = port
        self.daemon_token = daemon_token
        self.client_token = client_token
        self.client_token_verifier = client_token_verifier
        self.bound_port: int = port

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._start_error: Optional[Exception] = None

    def start(self, timeout: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(target=self._run_loop, name="pc-cli-ws-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise TimeoutError("pc_cli WebSocket 服务启动超时")
        if self._start_error:
            raise self._start_error

    def stop(self, timeout: float = 3.0) -> None:
        loop = self._loop
        if loop and not loop.is_closed():
            async def _shutdown():
                if self._server:
                    self._server.close()
                    await self._server.wait_closed()
                tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
                for t in tasks:
                    t.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            try:
                fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
                fut.result(timeout=timeout)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            server = loop.run_until_complete(
                asyncio.start_server(self._handle_tcp, host=self.host, port=self.port)
            )
            self._server = server
            if server.sockets:
                self.bound_port = server.sockets[0].getsockname()[1]
            self._ready.set()
            logger.info(f"pc_cli WebSocket 服务已启动: ws://{self.host}:{self.bound_port}")
            loop.run_forever()
        except Exception as e:
            self._start_error = e
            self._ready.set()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _reject_http(self, writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        resp = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Length: 0\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("iso-8859-1")
        try:
            writer.write(resp)
            await writer.drain()
        finally:
            writer.close()

    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw_hdr = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        except Exception:
            writer.close()
            return

        try:
            method, path, headers = _parse_http_request(raw_hdr)
        except Exception:
            await self._reject_http(writer, 400, "Bad Request")
            return

        if method != "GET" or headers.get("upgrade", "").lower() != "websocket":
            await self._reject_http(writer, 426, "Upgrade Required")
            return

        ws_key = headers.get("sec-websocket-key", "")
        if not ws_key:
            await self._reject_http(writer, 400, "Missing Sec-WebSocket-Key")
            return

        # 归一化 Nginx 透传路径
        norm_path = path
        if norm_path.startswith("/agent/"):
            norm_path = norm_path[6:]

        if norm_path == "/ws/pc-cli/daemon":
            if not self.daemon_token:
                await self._reject_http(writer, 503, "Daemon Token Not Configured")
                return
            supplied = _extract_bearer(headers)
            if not supplied or not hmac.compare_digest(supplied, self.daemon_token):
                await self._reject_http(writer, 401, "Unauthorized")
                return
            role = "daemon"
        elif norm_path == "/ws/pc-cli/client":
            if not self.client_token and self.client_token_verifier is None:
                await self._reject_http(writer, 503, "Client Token Not Configured")
                return
            role = "client"
        else:
            await self._reject_http(writer, 404, "Not Found")
            return

        # 完成 RFC 6455 握手
        accept = base64.b64encode(hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
        writer.write(handshake)
        await writer.drain()

        assert self._loop is not None
        conn = _WsConnection(reader, writer, self._loop)
        if role == "daemon":
            await self._serve_daemon(conn)
        else:
            pre_authed = self._verify_client_token(_extract_bearer(headers))
            await self._serve_client(conn, pre_authed=pre_authed)

    def _verify_client_token(self, token: str) -> bool:
        if not token:
            return False
        if self.client_token and hmac.compare_digest(token, self.client_token):
            return True
        if self.client_token_verifier is not None:
            try:
                return bool(self.client_token_verifier(token))
            except Exception:
                return False
        return False

    async def _serve_daemon(self, conn: _WsConnection) -> None:
        conn_id = f"dconn_{uuid.uuid4().hex[:10]}"
        node_id: Optional[str] = None
        try:
            while True:
                raw = await conn.recv_text()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == "node.hello":
                    node = self.hub.handle_daemon_hello(
                        conn_id,
                        lambda d: conn.send_json_threadsafe(d, wait_timeout=3.0),
                        msg,
                    )
                    node_id = node["node_id"]
                    await conn.send_json({"type": "hub.hello_ack", "node_id": node_id})
                elif not node_id:
                    await conn.send_json({"type": "error", "error": "must send node.hello first"})
                    break
                elif mtype == "node.heartbeat":
                    self.hub.handle_daemon_heartbeat(node_id, msg, conn_id=conn_id)
                elif mtype == "turn.event":
                    self.hub.handle_daemon_event(msg, sender_node_id=node_id, conn_id=conn_id)
                elif mtype == "turn.approval_request":
                    self.hub.handle_daemon_approval_request(msg, sender_node_id=node_id, conn_id=conn_id)
                elif mtype == "turn.approval_ack":
                    self.hub.handle_daemon_approval_ack(msg, sender_node_id=node_id, conn_id=conn_id)
                elif mtype == "turn.finished":
                    self.hub.handle_daemon_turn_finished(msg, sender_node_id=node_id, conn_id=conn_id)
                elif mtype == "dir.list_result":
                    self.hub.handle_daemon_dir_list_result(msg, sender_node_id=node_id, conn_id=conn_id)
        finally:
            if node_id:
                self.hub.handle_daemon_disconnect(node_id, conn_id)
            await conn.close()

    async def _serve_client(self, conn: _WsConnection, pre_authed: bool) -> None:
        client_id = f"cli_{uuid.uuid4().hex[:10]}"
        authed = pre_authed
        try:
            if not authed:
                raw = await asyncio.wait_for(conn.recv_text(), timeout=10.0)
                if raw is None:
                    return
                msg = json.loads(raw)
                token = str(msg.get("token") or "")
                if msg.get("type") != "auth" or not self._verify_client_token(token):
                    await conn.send_json({"type": "auth.error", "error": "Unauthorized"})
                    return
                authed = True
                await conn.send_json({"type": "auth.ok"})

            self.hub.register_client(client_id, conn.send_json_threadsafe, bot_id=None)
            while True:
                raw = await conn.recv_text()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("type") == "subscribe":
                    bot_id = msg.get("bot_id")
                    self.hub.update_client_subscription(client_id, str(bot_id) if bot_id else None)
                    await conn.send_json({"type": "subscribe.ok", "bot_id": bot_id})
                elif msg.get("type") == "ping":
                    await conn.send_json({"type": "pong"})
        except asyncio.TimeoutError:
            pass
        finally:
            if authed:
                self.hub.unregister_client(client_id)
            await conn.close()

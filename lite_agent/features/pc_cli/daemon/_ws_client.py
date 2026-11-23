"""
纯标准库 asyncio RFC 6455 WebSocket 客户端 (daemon._ws_client).
支持 ws:// 与 wss://、Bearer Token 鉴权、客户端帧掩码、分片重组与 ping/pong 保活。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import ssl
import struct
from typing import Any, Dict, Optional
from urllib.parse import urlparse

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_FRAME_BYTES = 4 * 1024 * 1024


class StdlibWsClient:
    """轻量零依赖 WebSocket 客户端。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self._closed = False
        self._write_lock = asyncio.Lock()

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 10.0,
    ) -> "StdlibWsClient":
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("ws", "wss"):
            raise ValueError(f"不支持的 WebSocket scheme: {scheme}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        ssl_ctx = ssl.create_default_context() if scheme == "wss" else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host=host, port=port, ssl=ssl_ctx, server_hostname=host if ssl_ctx else None),
            timeout=timeout,
        )

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = f"{host}:{port}" if parsed.port else host
        req_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in (headers or {}).items():
            req_lines.append(f"{k}: {v}")
        req_bytes = ("\r\n".join(req_lines) + "\r\n\r\n").encode("iso-8859-1")

        writer.write(req_bytes)
        await writer.drain()

        raw_resp = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        resp_lines = raw_resp.decode("iso-8859-1", errors="replace").split("\r\n")
        if not resp_lines or " 101 " not in resp_lines[0]:
            writer.close()
            raise ConnectionError(f"WebSocket 握手失败: {resp_lines[0] if resp_lines else 'empty'}")

        resp_headers: Dict[str, str] = {}
        for line in resp_lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                resp_headers[k.strip().lower()] = v.strip()
        expected_accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        if resp_headers.get("sec-websocket-accept") != expected_accept:
            writer.close()
            raise ConnectionError("WebSocket Sec-WebSocket-Accept 校验失败")

        return cls(reader, writer)

    async def send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return
        header = bytearray([0x80 | (opcode & 0x0F)])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
        async with self._write_lock:
            if self._closed:
                return
            self.writer.write(bytes(header) + mask_key + masked)
            await self.writer.drain()

    async def send_json(self, data: Dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        await self.send_frame(0x1, raw)

    async def recv_text(self) -> Optional[str]:
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
                length = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if length > _MAX_FRAME_BYTES:
                await self.close(1009)
                return None
            mask_key = await self.reader.readexactly(4) if masked else b""
            data = await self.reader.readexactly(length) if length > 0 else b""
            if masked and data:
                data = bytes(b ^ mask_key[i & 3] for i, b in enumerate(data))

            if opcode == 0x8:
                await self.close(1000)
                return None
            if opcode == 0x9:
                await self.send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x0):
                fragments.append(data)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")
        return None

    async def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.send_frame(0x8, struct.pack("!H", code))
        except Exception:
            pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass

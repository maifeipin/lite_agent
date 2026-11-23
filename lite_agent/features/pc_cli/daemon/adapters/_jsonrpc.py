"""
轻量 stdio JSON-RPC 2.0 客户端封装 (daemon.adapters._jsonrpc).
供 Codex `app-server` 与 AGY `acp` 共享，支持：
- 客户端 -> 服务端请求/响应（带唯一 id 与超时）
- 客户端 -> 服务端单向通知
- 服务端 -> 客户端通知回调
- 服务端 -> 客户端反向请求回调（如权限审批 requestApproval / session/request_permission）
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, Optional

NotificationHandler = Callable[[str, Dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class StdioJsonRpcClient:
    """基于行分隔 JSON-RPC 2.0 的异步子进程通信器。"""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        on_notification: Optional[NotificationHandler] = None,
        on_server_request: Optional[ServerRequestHandler] = None,
    ):
        self.proc = proc
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: Optional[asyncio.Task[Any]] = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        self._closed = True
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("JSON-RPC 会话已关闭"))
        self._pending.clear()
        for t in list(self._bg_tasks):
            t.cancel()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _write_json(self, obj: Dict[str, Any]) -> None:
        if self._closed or not self.proc.stdin:
            raise ConnectionError("子进程 stdin 不可写")
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Any:
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._write_json(payload)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_json(payload)

    async def _handle_server_request(self, req_id: Any, method: str, params: Dict[str, Any]) -> None:
        try:
            if self.on_server_request is None:
                await self._write_json({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unhandled server method: {method}"},
                })
                return
            result = await self.on_server_request(method, params)
            await self._write_json({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as e:
            try:
                await self._write_json({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": str(e)},
                })
            except Exception:
                pass

    async def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        try:
            while not self._closed:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").strip()
                if not text or not text.startswith("{"):
                    continue
                try:
                    msg = json.loads(text)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue

                has_id = "id" in msg and msg["id"] is not None
                has_method = isinstance(msg.get("method"), str)

                if has_id and not has_method:
                    # 对客户端请求的响应
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        if "error" in msg and msg["error"] is not None:
                            err = msg["error"]
                            emsg = err.get("message") if isinstance(err, dict) else str(err)
                            fut.set_exception(RuntimeError(emsg or "JSON-RPC error"))
                        else:
                            fut.set_result(msg.get("result"))
                elif has_id and has_method:
                    # 服务端反向请求（如权限审批），独立 Task 异步处理，不阻塞读循环
                    task = asyncio.create_task(
                        self._handle_server_request(msg["id"], msg["method"], msg.get("params") or {})
                    )
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)
                elif has_method and self.on_notification is not None:
                    try:
                        await self.on_notification(msg["method"], msg.get("params") or {})
                    except Exception:
                        pass
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError("子进程 stdout 已关闭"))

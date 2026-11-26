"""
Win/Mac 本地 PC CLI 守护进程主程序 (daemon.daemon).
主动通过 WebSocket 连接 VPS 中枢，上报可用引擎、处理目录查询、按 bot_id 串行调度 CLI 回合与中止。
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import sys
import uuid
from typing import Any, Dict, Optional

from ._ws_client import StdlibWsClient
from .adapters.agy_adapter import AgyCliAdapter
from .adapters.base import BaseCliAdapter
from .adapters.codex_adapter import CodexCliAdapter
from .config import DaemonConfig
from .env_probe import (
    build_engine_env,
    detect_available_engines,
    discover_engine_models,
    is_path_within_roots,
    list_safe_dirs,
)

logger = logging.getLogger("pc_cli.daemon")


class PcCliDaemon:
    """运行在 Windows / macOS 本机的轻量守护进程。"""

    def __init__(
        self,
        config: DaemonConfig,
        *,
        adapters: Optional[Dict[str, BaseCliAdapter]] = None,
    ):
        self.config = config
        self.env = build_engine_env(config.extra_path)
        self._custom_adapters = adapters is not None
        self.adapters: Dict[str, BaseCliAdapter] = adapters or {
            "codex": CodexCliAdapter(
                executable=config.codex_bin,
                env=self.env,
                inactivity_timeout_sec=config.inactivity_timeout_sec,
                turn_timeout_sec=config.turn_timeout_sec,
            ),
            "agy": AgyCliAdapter(
                executable=config.agy_bin,
                env=self.env,
                inactivity_timeout_sec=config.inactivity_timeout_sec,
                turn_timeout_sec=config.turn_timeout_sec,
            ),
        }
        self._bot_locks: Dict[str, asyncio.Lock] = {}
        self._active_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._active_turns: Dict[str, str] = {}  # bot_id -> turn_id
        self._active_engines: Dict[str, str] = {}  # bot_id -> engine
        self._pending_approvals: Dict[str, asyncio.Future[str]] = {}  # approval_id -> Future[decision]
        self._turn_approval_ids: Dict[tuple[str, str], set[str]] = {}  # (bot_id, turn_id) -> set[approval_id]
        self._cancelled_turn_ids: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def _get_bot_lock(self, bot_id: str) -> asyncio.Lock:
        lock = self._bot_locks.get(bot_id)
        if lock is None:
            lock = asyncio.Lock()
            self._bot_locks[bot_id] = lock
        return lock

    async def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                await self.run_once()
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"与中枢连接断开 ({e})，{backoff:.1f}s 后重连...")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2.0, 30.0)

    async def run_once(self) -> None:
        if not self.config.daemon_token:
            raise ValueError("未配置 PC_CLI_DAEMON_TOKEN，拒绝连接中枢")
        ws = await StdlibWsClient.connect(
            self.config.hub_ws_url,
            headers={"Authorization": f"Bearer {self.config.daemon_token}"},
        )
        hb_task: Optional[asyncio.Task[Any]] = None
        try:
            engines = (
                list(self.adapters.keys())
                if self._custom_adapters
                else detect_available_engines(self.config.codex_bin, self.config.agy_bin, self.env)
            )
            engine_models = (
                {}
                if self._custom_adapters
                else discover_engine_models(engines, self.env)
            )
            await ws.send_json({
                "type": "node.hello",
                "node_id": self.config.node_id,
                "display_name": self.config.display_name,
                "platform": sys.platform,
                "engines": engines,
                "engine_models": engine_models,
                "workspace_roots": self.config.workspace_roots,
                "active_turns": list(self._active_turns.values()),
            })

            async def _heartbeat() -> None:
                while not self._stop_event.is_set():
                    await asyncio.sleep(self.config.heartbeat_interval_sec)
                    await ws.send_json({
                        "type": "node.heartbeat",
                        "node_id": self.config.node_id,
                        "active_turns": list(self._active_turns.values()),
                    })

            hb_task = asyncio.create_task(_heartbeat())
            while not self._stop_event.is_set():
                recv_task = asyncio.create_task(ws.recv_text())
                stop_task = asyncio.create_task(self._stop_event.wait())
                done, pending = await asyncio.wait(
                    {recv_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for p in pending:
                    p.cancel()
                if stop_task in done:
                    break
                raw = recv_task.result()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                await self._dispatch(ws, msg)
        finally:
            # 断线时释放所有挂起中的审批等待，默认拒绝（fail-closed）
            for aid, fut in list(self._pending_approvals.items()):
                if not fut.done():
                    fut.set_result("deny")
            self._pending_approvals.clear()
            self._turn_approval_ids.clear()
            if hb_task:
                hb_task.cancel()
            await ws.close()

    async def _dispatch(self, ws: StdlibWsClient, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "dir.list_request":
            req_id = str(msg.get("req_id") or "")
            req_path = str(msg.get("path") or "")
            ok, entries, err = list_safe_dirs(req_path, self.config.workspace_roots)
            await ws.send_json({
                "type": "dir.list_result",
                "req_id": req_id,
                "entries": entries if ok else [],
                "error": err if not ok else "",
            })
        elif mtype == "turn.start":
            bot_id = str(msg.get("bot_id") or "")
            task = asyncio.create_task(self._run_turn_task(ws, msg))
            self._active_tasks[bot_id] = task
        elif mtype == "turn.cancel":
            bot_id = str(msg.get("bot_id") or "")
            req_tid = str(msg.get("turn_id") or "")
            if req_tid:
                self._cancelled_turn_ids[req_tid] = None
                while len(self._cancelled_turn_ids) > 256:
                    self._cancelled_turn_ids.popitem(last=False)
            active_tid = self._active_turns.get(bot_id)
            if not active_tid:
                return
            # 强校验 turn_id：旧回合的迟到 cancel 绝不得影响当前新回合的审批与子进程
            if req_tid and req_tid != active_tid:
                logger.info(f"忽略旧回合迟到中止指令: bot={bot_id} req_turn={req_tid} active_turn={active_tid}")
                return
            for aid in list(self._turn_approval_ids.get((bot_id, active_tid), ())):
                fut = self._pending_approvals.get(aid)
                if fut and not fut.done():
                    fut.set_result("deny")
            engine = self._active_engines.get(bot_id)
            if engine and engine in self.adapters:
                await self.adapters[engine].cancel_turn(bot_id, active_tid)
        elif mtype == "turn.approval_decision":
            bot_id = str(msg.get("bot_id") or "")
            turn_id = str(msg.get("turn_id") or "")
            approval_id = str(msg.get("approval_id") or "")
            decision = str(msg.get("decision") or "deny")
            fut = self._pending_approvals.get(approval_id)
            active_tid = self._active_turns.get(bot_id)
            if fut and not fut.done() and (not turn_id or active_tid == turn_id):
                fut.set_result(decision)
                await ws.send_json({
                    "type": "turn.approval_ack",
                    "bot_id": bot_id,
                    "turn_id": turn_id or active_tid or "",
                    "approval_id": approval_id,
                    "accepted": True,
                })
            else:
                await ws.send_json({
                    "type": "turn.approval_ack",
                    "bot_id": bot_id,
                    "turn_id": turn_id,
                    "approval_id": approval_id,
                    "accepted": False,
                    "reason": "approval_not_pending_on_daemon",
                })

    async def _run_turn_task(self, ws: StdlibWsClient, msg: Dict[str, Any]) -> None:
        bot_id = str(msg.get("bot_id") or "")
        turn_id = str(msg.get("turn_id") or "")
        engine = str(msg.get("engine") or "")
        cwd = str(msg.get("cwd") or "")
        prompt = str(msg.get("prompt") or "")
        native_session_id = msg.get("native_session_id")
        approval_mode = str(msg.get("approval_mode") or "auto")
        execution_mode = str(msg.get("execution_mode") or "agent")
        reasoning_effort = str(msg.get("reasoning_effort") or "")
        model = msg.get("model")

        if engine not in self.adapters:
            await ws.send_json({
                "type": "turn.finished",
                "bot_id": bot_id,
                "turn_id": turn_id,
                "status": "failed",
                "error": f"不支持的引擎: {engine}",
            })
            return

        if not is_path_within_roots(cwd, self.config.workspace_roots):
            await ws.send_json({
                "type": "turn.finished",
                "bot_id": bot_id,
                "turn_id": turn_id,
                "status": "failed",
                "error": f"工作目录不在允许的白名单内: {cwd}",
            })
            return

        lock = self._get_bot_lock(bot_id)
        async with lock:
            if turn_id and turn_id in self._cancelled_turn_ids:
                self._cancelled_turn_ids.pop(turn_id, None)
                await ws.send_json({
                    "type": "turn.finished",
                    "bot_id": bot_id,
                    "turn_id": turn_id,
                    "status": "cancelled",
                })
                return
            self._active_turns[bot_id] = turn_id
            self._active_engines[bot_id] = engine
            try:
                async def _on_event(event_type: str, payload: Dict[str, Any]) -> None:
                    await ws.send_json({
                        "type": "turn.event",
                        "bot_id": bot_id,
                        "turn_id": turn_id,
                        "event_type": event_type,
                        "payload": payload,
                    })

                async def _on_approval(req: Dict[str, Any]) -> str:
                    aid = str(req.get("approval_id") or f"appr_{uuid.uuid4().hex[:12]}")
                    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
                    self._pending_approvals[aid] = fut
                    turn_key = (bot_id, turn_id)
                    self._turn_approval_ids.setdefault(turn_key, set()).add(aid)
                    try:
                        await ws.send_json({
                            "type": "turn.approval_request",
                            "bot_id": bot_id,
                            "turn_id": turn_id,
                            "approval_id": aid,
                            "tool_name": str(req.get("tool_name") or "tool"),
                            "summary": str(req.get("summary") or "请求执行敏感操作"),
                            "raw_request": req.get("raw_request") or {},
                        })
                        return await asyncio.wait_for(fut, timeout=self.config.turn_timeout_sec)
                    except asyncio.TimeoutError:
                        return "deny"
                    finally:
                        self._pending_approvals.pop(aid, None)
                        ids = self._turn_approval_ids.get(turn_key)
                        if ids:
                            ids.discard(aid)
                            if not ids:
                                self._turn_approval_ids.pop(turn_key, None)

                result = await self.adapters[engine].run_turn(
                    bot_id=bot_id,
                    turn_id=turn_id,
                    cwd=cwd,
                    prompt=prompt,
                    native_session_id=str(native_session_id) if native_session_id else None,
                    approval_mode=approval_mode,
                    model=str(model) if model else None,
                    on_event=_on_event,
                    on_approval=_on_approval,
                    execution_mode=execution_mode,
                    reasoning_effort=reasoning_effort,
                )
                await ws.send_json({
                    "type": "turn.finished",
                    "bot_id": bot_id,
                    "turn_id": turn_id,
                    "status": result.status,
                    "native_session_id": result.native_session_id,
                    "error": result.error,
                })
            except Exception as e:
                await ws.send_json({
                    "type": "turn.finished",
                    "bot_id": bot_id,
                    "turn_id": turn_id,
                    "status": "failed",
                    "error": str(e),
                })
            finally:
                self._active_turns.pop(bot_id, None)
                self._active_engines.pop(bot_id, None)
                self._active_tasks.pop(bot_id, None)

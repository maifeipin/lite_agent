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
from .adapters.base import BaseCliAdapter, apply_bot_system_prompt
from .adapters.codex_adapter import CodexCliAdapter
from .config import DaemonConfig
from .env_probe import (
    PinnedWorkspaceRoot,
    build_engine_env,
    close_pinned_roots,
    detect_available_engines,
    discover_engine_models,
    list_safe_dirs,
    pin_workspace_roots,
    resolve_safe_cwd,
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
        self._staged_commits: Dict[str, asyncio.Future[bool]] = {}  # turn_id -> Future[committed]
        self._committed_turns: set[str] = set()  # 已收到 turn.commit、尚未收尾的 turn_id
        self._cancelled_turn_ids: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        self._stop_event = asyncio.Event()
        # 在载入配置时一次性固定白名单根目录身份（路径 + st_dev/st_ino + POSIX dir_fd），目录探测与执行前校验共用
        self._pinned_roots: list[PinnedWorkspaceRoot] = pin_workspace_roots(config.workspace_roots)

    def close(self) -> None:
        close_pinned_roots(self._pinned_roots)

    def __del__(self) -> None:
        try:
            close_pinned_roots(self._pinned_roots)
        except Exception:
            pass

    def _snapshot_active_turns(self) -> list[str]:
        return list(dict.fromkeys([*self._active_turns.values(), *self._committed_turns]))

    @staticmethod
    async def _safe_send_json(ws: StdlibWsClient, payload: Dict[str, Any]) -> None:
        try:
            await ws.send_json(payload)
        except Exception:
            pass

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
                "active_turns": self._snapshot_active_turns(),
                "capabilities": ["turn_two_phase"],
            })

            async def _heartbeat() -> None:
                while not self._stop_event.is_set():
                    await asyncio.sleep(self.config.heartbeat_interval_sec)
                    await ws.send_json({
                        "type": "node.heartbeat",
                        "node_id": self.config.node_id,
                        "active_turns": self._snapshot_active_turns(),
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
            # 断线时释放所有挂起中的审批等待与待提交阶段锁，默认拒绝（fail-closed）
            for aid, fut in list(self._pending_approvals.items()):
                if not fut.done():
                    fut.set_result("deny")
            self._pending_approvals.clear()
            self._turn_approval_ids.clear()
            for tid, cfut in list(self._staged_commits.items()):
                if not cfut.done():
                    cfut.set_result(False)
            self._staged_commits.clear()
            if hb_task:
                hb_task.cancel()
            await ws.close()

    async def _dispatch(self, ws: StdlibWsClient, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "dir.list_request":
            req_id = str(msg.get("req_id") or "")
            req_path = str(msg.get("path") or "")
            create_if_missing = bool(msg.get("create_if_missing"))
            ok, entries, err = list_safe_dirs(
                req_path,
                self.config.workspace_roots,
                create_if_missing=create_if_missing,
                pinned_roots=self._pinned_roots,
            )
            await ws.send_json({
                "type": "dir.list_result",
                "req_id": req_id,
                "entries": entries if ok else [],
                "error": err if not ok else "",
            })
        elif mtype == "turn.start":
            bot_id = str(msg.get("bot_id") or "")
            turn_id = str(msg.get("turn_id") or "")
            require_commit = bool(msg.get("require_commit"))
            commit_fut: Optional[asyncio.Future[bool]] = None
            if require_commit and turn_id:
                commit_fut = asyncio.get_running_loop().create_future()
                if turn_id in self._cancelled_turn_ids:
                    self._cancelled_turn_ids.pop(turn_id, None)
                    commit_fut.set_result(False)
                else:
                    self._staged_commits[turn_id] = commit_fut
            task = asyncio.create_task(self._run_turn_task(ws, msg, commit_fut=commit_fut))
            self._active_tasks[bot_id] = task
            if require_commit and turn_id and commit_fut is not None and not commit_fut.done():
                await ws.send_json({
                    "type": "turn.prepared",
                    "bot_id": bot_id,
                    "turn_id": turn_id,
                })
        elif mtype == "turn.commit":
            req_tid = str(msg.get("turn_id") or "")
            cfut = self._staged_commits.get(req_tid)
            if cfut and not cfut.done():
                if req_tid in self._cancelled_turn_ids:
                    self._cancelled_turn_ids.pop(req_tid, None)
                    cfut.set_result(False)
                else:
                    if req_tid:
                        self._committed_turns.add(req_tid)
                    cfut.set_result(True)
        elif mtype == "turn.cancel":
            bot_id = str(msg.get("bot_id") or "")
            req_tid = str(msg.get("turn_id") or "")
            if req_tid:
                self._cancelled_turn_ids[req_tid] = None
                while len(self._cancelled_turn_ids) > 256:
                    self._cancelled_turn_ids.popitem(last=False)
                self._committed_turns.discard(req_tid)
                cfut = self._staged_commits.get(req_tid)
                if cfut and not cfut.done():
                    cfut.set_result(False)
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

    async def _run_turn_task(
        self,
        ws: StdlibWsClient,
        msg: Dict[str, Any],
        *,
        commit_fut: Optional[asyncio.Future[bool]] = None,
    ) -> None:
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
        system_prompt = str(msg.get("system_prompt") or "")
        effective_prompt = apply_bot_system_prompt(
            prompt,
            system_prompt,
            native_session_id=str(native_session_id) if native_session_id else None,
        )

        try:
            # 两阶段授权：在收到 Hub 显式 turn.commit 前严禁抢锁或启动底层 CLI（即便事件循环已多次切换）
            if commit_fut is not None:
                try:
                    committed = await asyncio.wait_for(commit_fut, timeout=15.0)
                except asyncio.TimeoutError:
                    committed = False
                finally:
                    self._staged_commits.pop(turn_id, None)
                if not committed:
                    await self._safe_send_json(ws, {
                        "type": "turn.finished",
                        "bot_id": bot_id,
                        "turn_id": turn_id,
                        "status": "cancelled",
                    })
                    return

            if engine not in self.adapters:
                await self._safe_send_json(ws, {
                    "type": "turn.finished",
                    "bot_id": bot_id,
                    "turn_id": turn_id,
                    "status": "failed",
                    "error": f"不支持的引擎: {engine}",
                })
                return

            safe_cwd = resolve_safe_cwd(
                cwd,
                self.config.workspace_roots,
                pinned_roots=self._pinned_roots,
            )
            if not safe_cwd:
                await self._safe_send_json(ws, {
                    "type": "turn.finished",
                    "bot_id": bot_id,
                    "turn_id": turn_id,
                    "status": "failed",
                    "error": f"工作目录不存在或不在允许的白名单内: {cwd}",
                })
                return

            lock = self._get_bot_lock(bot_id)
            async with lock:
                if turn_id and turn_id in self._cancelled_turn_ids:
                    self._cancelled_turn_ids.pop(turn_id, None)
                    await self._safe_send_json(ws, {
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
                        cwd=safe_cwd,
                        prompt=effective_prompt,
                        native_session_id=str(native_session_id) if native_session_id else None,
                        approval_mode=approval_mode,
                        model=str(model) if model else None,
                        on_event=_on_event,
                        on_approval=_on_approval,
                        execution_mode=execution_mode,
                        reasoning_effort=reasoning_effort,
                    )
                    if result.status == "failed":
                        logger.warning(f"回合执行失败 bot={bot_id} turn={turn_id} engine={engine}: {result.error}")
                    await self._safe_send_json(ws, {
                        "type": "turn.finished",
                        "bot_id": bot_id,
                        "turn_id": turn_id,
                        "status": result.status,
                        "native_session_id": result.native_session_id,
                        "error": result.error,
                    })
                except Exception as e:
                    logger.warning(f"回合执行异常 bot={bot_id} turn={turn_id} engine={engine}: {e}")
                    await self._safe_send_json(ws, {
                        "type": "turn.finished",
                        "bot_id": bot_id,
                        "turn_id": turn_id,
                        "status": "failed",
                        "error": str(e),
                    })
                finally:
                    if self._active_turns.get(bot_id) == turn_id:
                        self._active_turns.pop(bot_id, None)
                        self._active_engines.pop(bot_id, None)
        finally:
            if turn_id:
                self._staged_commits.pop(turn_id, None)
                self._committed_turns.discard(turn_id)
            if self._active_tasks.get(bot_id) is asyncio.current_task():
                self._active_tasks.pop(bot_id, None)

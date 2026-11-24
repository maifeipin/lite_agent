"""
Codex CLI 适配器 (daemon.adapters.codex_adapter).
Phase 1: 基于 `codex exec --json` / `codex exec resume <thread_id> --json` 流式解析 JSONL。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

from ..env_probe import build_spawn_argv
from ..process_tree import build_subprocess_kwargs, kill_process_tree
from ._jsonrpc import StdioJsonRpcClient
from .base import ApprovalCallback, BaseCliAdapter, EventCallback, TurnResult, truncate_text

_CODEX_APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval",
    "execCommandApproval",
    "item/fileChange/requestApproval",
    "applyPatchApproval",
    "item/permissions/requestApproval",
})


def is_model_resume_notice(message: str) -> bool:
    """Codex 将恢复模型差异也编码为 error 事件；仅降级这一种已知提示。"""
    return (
        message.startswith("This session was recorded with model ")
        and " but is resuming with " in message
        and "Consider switching back to " in message
    )


def map_decision_to_codex(decision: str) -> str:
    if decision == "allow_once":
        return "accept"
    if decision == "allow_session":
        return "acceptForSession"
    return "decline"


def build_codex_permissions_response(decision: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """按 Codex App Server 协议构造 item/permissions/requestApproval 响应（permissions + scope）。"""
    requested_perms = params.get("permissions")
    if decision in ("allow_once", "allow_session") and isinstance(requested_perms, dict):
        scope = "session" if decision == "allow_session" else "turn"
        return {"permissions": requested_perms, "scope": scope}
    return {"permissions": {}, "scope": "turn"}


def parse_codex_json_line(line: str) -> Optional[Dict[str, Any]]:
    text = line.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def extract_codex_tool_payload(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    itype = str(item.get("type") or "")
    if itype in ("agent_message", "reasoning", "user_message", "error", ""):
        return None
    tool_id = str(item.get("id") or "")
    if itype == "command_execution":
        return {
            "tool_id": tool_id,
            "tool_name": "command_execution",
            "command": truncate_text(item.get("command") or "", 2000),
            "output": truncate_text(item.get("aggregated_output") or item.get("output") or ""),
            "exit_code": item.get("exit_code"),
            "is_error": bool(item.get("exit_code") not in (None, 0) or item.get("status") == "failed"),
        }
    if itype == "file_change":
        return {
            "tool_id": tool_id,
            "tool_name": "file_change",
            "changes": item.get("changes") or [],
            "is_error": item.get("status") == "failed",
        }
    return {
        "tool_id": tool_id,
        "tool_name": str(item.get("tool") or item.get("name") or itype),
        "output": truncate_text(item.get("result") or item.get("output") or item.get("error") or ""),
        "is_error": bool(item.get("error") or item.get("status") == "failed"),
    }


class CodexCliAdapter(BaseCliAdapter):
    """Codex CLI Phase 1 流式 JSONL 适配器。"""

    def __init__(
        self,
        *,
        executable: str = "codex",
        env: Dict[str, str],
        inactivity_timeout_sec: float = 1800.0,
        turn_timeout_sec: float = 7200.0,
        prefer_app_server: bool = False,
    ):
        self.executable = executable
        self.env = env
        self.inactivity_timeout_sec = inactivity_timeout_sec
        self.turn_timeout_sec = turn_timeout_sec
        self.prefer_app_server = prefer_app_server
        self._active_procs: Dict[Tuple[str, str], asyncio.subprocess.Process] = {}
        self._active_rpcs: Dict[Tuple[str, str], Tuple[StdioJsonRpcClient, Optional[str], Optional[str]]] = {}
        self._cancelled: set[Tuple[str, str]] = set()

    def build_app_server_args(self) -> List[str]:
        return build_spawn_argv(self.executable, ["app-server"], self.env)

    def build_args(
        self,
        *,
        native_session_id: Optional[str],
        approval_mode: str,
        model: Optional[str],
    ) -> List[str]:
        """借鉴 tarobird process-adapter.ts：加 --skip-git-repo-check，prompt 走 stdin (`-`)。
        codex-cli 0.171+ 已移除 `--full-auto`，统一使用 `-c approval_policy="never" -c sandbox_mode="workspace-write"`
        以同时兼容 `codex exec` 与 `codex exec resume`。
        """
        flags: List[str] = []
        if approval_mode == "auto":
            flags.extend(["-c", 'approval_policy="never"', "-c", 'sandbox_mode="workspace-write"'])
        if model:
            flags.extend(["-m", model])
        if native_session_id:
            cli_args = ["exec", "resume", "--json", "--skip-git-repo-check", "--all", *flags, native_session_id, "-"]
        else:
            cli_args = ["exec", "--json", "--skip-git-repo-check", *flags, "-"]
        return build_spawn_argv(self.executable, cli_args, self.env)

    async def cancel_turn(self, bot_id: str, turn_id: str) -> None:
        key = (bot_id, turn_id)
        self._cancelled.add(key)
        rpc_info = self._active_rpcs.get(key)
        if rpc_info is not None:
            rpc, th_id, native_tid = rpc_info
            if th_id and native_tid:
                try:
                    await rpc.call("turn/interrupt", {"threadId": th_id, "turnId": native_tid}, timeout=3.0)
                except Exception:
                    pass
        proc = self._active_procs.get(key)
        if proc is not None:
            await kill_process_tree(proc)

    async def run_turn(
        self,
        *,
        bot_id: str,
        turn_id: str,
        cwd: str,
        prompt: str,
        native_session_id: Optional[str],
        approval_mode: str,
        model: Optional[str],
        on_event: EventCallback,
        on_approval: Optional[ApprovalCallback] = None,
    ) -> TurnResult:
        if approval_mode == "ask" or self.prefer_app_server:
            return await self._run_app_server_turn(
                bot_id=bot_id,
                turn_id=turn_id,
                cwd=cwd,
                prompt=prompt,
                native_session_id=native_session_id,
                approval_mode=approval_mode,
                model=model,
                on_event=on_event,
                on_approval=on_approval,
            )
        return await self._run_exec_turn(
            bot_id=bot_id,
            turn_id=turn_id,
            cwd=cwd,
            prompt=prompt,
            native_session_id=native_session_id,
            approval_mode=approval_mode,
            model=model,
            on_event=on_event,
        )

    async def _run_app_server_turn(
        self,
        *,
        bot_id: str,
        turn_id: str,
        cwd: str,
        prompt: str,
        native_session_id: Optional[str],
        approval_mode: str,
        model: Optional[str],
        on_event: EventCallback,
        on_approval: Optional[ApprovalCallback],
    ) -> TurnResult:
        """Phase 3: 基于 `codex app-server` JSON-RPC 2.0，支持交互式权限审批与优雅中断。"""
        key = (bot_id, turn_id)
        self._cancelled.discard(key)
        try:
            args = self.build_app_server_args()
        except Exception as e:
            return TurnResult(status="failed", native_session_id=native_session_id, error=str(e))
        kwargs = build_subprocess_kwargs(cwd=cwd, env=self.env)
        try:
            proc = await asyncio.create_subprocess_exec(*args, **kwargs)
        except Exception as e:
            return TurnResult(status="failed", native_session_id=native_session_id, error=f"启动 codex app-server 失败: {e}")

        self._active_procs[key] = proc
        thread_id = native_session_id
        native_turn_id: Optional[str] = None
        streamed_parts: List[str] = []
        last_message = ""
        turn_error: Optional[str] = None
        turn_status = "completed"
        turn_done = asyncio.Event()

        async def _on_notification(method: str, params: Dict[str, Any]) -> None:
            nonlocal last_message, turn_error, turn_status
            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                if delta:
                    streamed_parts.append(delta)
                    await on_event("text_delta", {"delta": delta})
            elif method == "item/completed":
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                itype = str(item.get("type") or "")
                if itype == "reasoning" and item.get("text"):
                    await on_event("thinking", {"text": truncate_text(item["text"])})
                elif itype in ("agent_message", "agentMessage") and item.get("text"):
                    last_message = str(item["text"])
                    await on_event("assistant_message", {"text": truncate_text(last_message)})
                elif itype == "error":
                    msg = str(item.get("message") or item.get("text") or "").strip()
                    if msg:
                        await on_event("error", {"message": truncate_text(msg, 2000), "non_fatal": True})
                else:
                    tool_payload = extract_codex_tool_payload(item)
                    if tool_payload:
                        await on_event("tool_start", {
                            "tool_id": tool_payload["tool_id"],
                            "tool_name": tool_payload["tool_name"],
                            "command": tool_payload.get("command", ""),
                        })
                        await on_event("tool_end", tool_payload)
            elif method == "turn/completed":
                t_obj = params.get("turn") if isinstance(params.get("turn"), dict) else params
                st = str(t_obj.get("status") or "completed")
                if st in ("interrupted", "cancelled"):
                    turn_status = "cancelled"
                elif st == "failed":
                    turn_status = "failed"
                    err = t_obj.get("error") or {}
                    turn_error = str(err.get("message") if isinstance(err, dict) else err) or "Codex turn failed"
                else:
                    turn_status = "completed"
                turn_done.set()
            elif method in ("turn/failed", "error"):
                err = params.get("error") or params
                message = str(err.get("message") if isinstance(err, dict) else err) or "Codex app-server error"
                if method == "error" and (is_model_resume_notice(message) or params.get("willRetry") is True):
                    await on_event("warning", {"message": message, "non_fatal": True})
                    return
                turn_error = message
                turn_status = "failed"
                await on_event("error", {"message": turn_error})
                turn_done.set()

        async def _on_server_request(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            if method in _CODEX_APPROVAL_METHODS:
                if approval_mode != "ask" or on_approval is None:
                    # 缺省安全：未提供审批回调时绝不自动放行
                    decision = "allow_once" if approval_mode == "auto" else "deny"
                else:
                    cmd = params.get("command") or params.get("parsedCmd") or params.get("reason") or method
                    tool_name = "command_execution" if "command" in method.lower() or "exec" in method.lower() else (
                        "file_change" if "file" in method.lower() or "patch" in method.lower() else method
                    )
                    summary = truncate_text(cmd, 500)
                    decision = await on_approval({
                        "tool_name": tool_name,
                        "summary": summary,
                        "raw_request": params,
                    })
                if method == "item/permissions/requestApproval":
                    return build_codex_permissions_response(decision, params)
                return {"decision": map_decision_to_codex(decision)}
            return {}

        rpc = StdioJsonRpcClient(proc, on_notification=_on_notification, on_server_request=_on_server_request)
        rpc.start()
        try:
            async def _execute_rpc_turn() -> None:
                nonlocal thread_id, native_turn_id
                await rpc.call(
                    "initialize",
                    {"clientInfo": {"name": "lite-agent-pc-cli", "version": "1.0.0"}},
                    timeout=30.0,
                )
                await rpc.notify("initialized", {})

                policy = "on-request" if approval_mode == "ask" else "never"
                if thread_id:
                    resume_params: Dict[str, Any] = {
                        "threadId": thread_id, "cwd": cwd, "approvalPolicy": policy,
                        "sandbox": "workspace-write",
                    }
                    if model:
                        resume_params["model"] = model
                    th_res = await rpc.call(
                        "thread/resume",
                        resume_params,
                        timeout=30.0,
                    )
                else:
                    start_params: Dict[str, Any] = {"cwd": cwd, "approvalPolicy": policy, "sandbox": "workspace-write"}
                    if model:
                        start_params["model"] = model
                    th_res = await rpc.call("thread/start", start_params, timeout=30.0)
                if isinstance(th_res, dict):
                    th_obj = th_res.get("thread") if isinstance(th_res.get("thread"), dict) else th_res
                    if th_obj.get("id"):
                        thread_id = str(th_obj["id"])

                turn_res = await rpc.call(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": prompt}],
                    },
                    timeout=30.0,
                )
                if isinstance(turn_res, dict):
                    t_obj = turn_res.get("turn") if isinstance(turn_res.get("turn"), dict) else turn_res
                    if t_obj.get("id"):
                        native_turn_id = str(t_obj["id"])
                self._active_rpcs[key] = (rpc, thread_id, native_turn_id)

                await turn_done.wait()

            await asyncio.wait_for(_execute_rpc_turn(), timeout=self.turn_timeout_sec)

            answer = last_message or "".join(streamed_parts)
            if answer and not last_message:
                await on_event("assistant_message", {"text": truncate_text(answer)})

            if key in self._cancelled or turn_status == "cancelled":
                return TurnResult(status="cancelled", native_session_id=thread_id, final_text=answer)
            if turn_error or turn_status == "failed":
                return TurnResult(status="failed", native_session_id=thread_id, final_text=answer, error=turn_error or "Codex failed")
            return TurnResult(status="completed", native_session_id=thread_id, final_text=answer)
        except asyncio.TimeoutError:
            await kill_process_tree(proc)
            return TurnResult(status="failed", native_session_id=thread_id, error="codex app-server 执行超时已强制终止")
        except Exception as e:
            if key in self._cancelled:
                return TurnResult(status="cancelled", native_session_id=thread_id)
            return TurnResult(status="failed", native_session_id=thread_id, error=f"codex app-server 异常: {e}")
        finally:
            self._active_rpcs.pop(key, None)
            self._active_procs.pop(key, None)
            self._cancelled.discard(key)
            await rpc.stop()
            await kill_process_tree(proc)

    async def _run_exec_turn(
        self,
        *,
        bot_id: str,
        turn_id: str,
        cwd: str,
        prompt: str,
        native_session_id: Optional[str],
        approval_mode: str,
        model: Optional[str],
        on_event: EventCallback,
    ) -> TurnResult:
        key = (bot_id, turn_id)
        self._cancelled.discard(key)
        args = self.build_args(
            native_session_id=native_session_id,
            approval_mode=approval_mode,
            model=model,
        )
        kwargs = build_subprocess_kwargs(cwd=cwd, env=self.env)
        try:
            proc = await asyncio.create_subprocess_exec(*args, **kwargs)
        except Exception as e:
            return TurnResult(status="failed", native_session_id=native_session_id, error=f"启动 codex 失败: {e}")

        self._active_procs[key] = proc
        thread_id = native_session_id
        last_message = ""
        last_error: Optional[str] = None
        turn_completed = False
        stderr_chunks: List[str] = []

        async def _drain_stderr() -> None:
            if not proc.stderr:
                return
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                if len(stderr_chunks) < 16:
                    stderr_chunks.append(chunk.decode("utf-8", errors="replace"))

        stderr_task = asyncio.create_task(_drain_stderr())

        try:
            async def _execute_full_turn() -> int:
                nonlocal thread_id, last_message, last_error, turn_completed
                if proc.stdin:
                    proc.stdin.write((prompt + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()

                assert proc.stdout is not None
                while True:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=self.inactivity_timeout_sec)
                    if not raw:
                        break
                    ev = parse_codex_json_line(raw.decode("utf-8", errors="replace"))
                    if not ev:
                        continue
                    etype = str(ev.get("type") or "")
                    if etype == "thread.started" and ev.get("thread_id"):
                        thread_id = str(ev["thread_id"])
                    elif etype == "item.completed" and isinstance(ev.get("item"), dict):
                        item = ev["item"]
                        itype = str(item.get("type") or "")
                        if itype == "reasoning" and item.get("text"):
                            await on_event("thinking", {"text": truncate_text(item["text"])})
                        elif itype == "agent_message" and item.get("text"):
                            last_message = str(item["text"])
                            await on_event("assistant_message", {"text": truncate_text(last_message)})
                        elif itype == "error":
                            msg = str(item.get("message") or item.get("text") or "").strip()
                            if msg:
                                await on_event("error", {"message": truncate_text(msg, 2000), "non_fatal": True})
                        else:
                            tool_payload = extract_codex_tool_payload(item)
                            if tool_payload:
                                await on_event("tool_start", {
                                    "tool_id": tool_payload["tool_id"],
                                    "tool_name": tool_payload["tool_name"],
                                    "command": tool_payload.get("command", ""),
                                })
                                await on_event("tool_end", tool_payload)
                    elif etype == "turn.failed":
                        err = ev.get("error") or {}
                        last_error = str(err.get("message") if isinstance(err, dict) else err) or "Codex turn failed"
                        await on_event("error", {"message": last_error})
                    elif etype == "error":
                        message = str(ev.get("message") or "Codex error")
                        if is_model_resume_notice(message):
                            await on_event("warning", {"message": message, "non_fatal": True})
                        else:
                            last_error = message
                            await on_event("error", {"message": last_error})
                    elif etype == "turn.completed":
                        turn_completed = True

                rc_inner = await proc.wait()
                try:
                    await asyncio.wait_for(stderr_task, timeout=2.0)
                except asyncio.TimeoutError:
                    stderr_task.cancel()
                return rc_inner

            rc = await asyncio.wait_for(_execute_full_turn(), timeout=self.turn_timeout_sec)

            if key in self._cancelled:
                return TurnResult(status="cancelled", native_session_id=thread_id, final_text=last_message)
            if last_error:
                return TurnResult(status="failed", native_session_id=thread_id, final_text=last_message, error=last_error)
            if rc != 0:
                diag = "".join(stderr_chunks).strip()[-1000:]
                return TurnResult(
                    status="failed",
                    native_session_id=thread_id,
                    final_text=last_message,
                    error=f"codex 退出码 {rc}" + (f": {diag}" if diag else ""),
                )
            if not turn_completed:
                return TurnResult(status="failed", native_session_id=thread_id, final_text=last_message,
                                  error="Codex 进程已退出，但未收到 turn.completed 完成事件")
            return TurnResult(status="completed", native_session_id=thread_id, final_text=last_message)
        except asyncio.TimeoutError:
            await kill_process_tree(proc)
            return TurnResult(status="failed", native_session_id=thread_id, error="codex 执行超时已强制终止")
        finally:
            self._active_procs.pop(key, None)
            self._cancelled.discard(key)
            if not stderr_task.done():
                stderr_task.cancel()
            await kill_process_tree(proc)

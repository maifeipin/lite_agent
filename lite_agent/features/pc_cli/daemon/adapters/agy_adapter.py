"""
AGY (Antigravity) CLI 适配器 (daemon.adapters.agy_adapter).
Phase 1: 基于 `agy -p ... --output-format stream-json` 流式解析 JSONL。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..env_probe import build_spawn_argv
from ..process_tree import build_subprocess_kwargs, kill_process_tree
from ._jsonrpc import StdioJsonRpcClient
from .base import ApprovalCallback, BaseCliAdapter, EventCallback, TurnResult, truncate_text

_AGY_CONV_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def select_acp_permission_outcome(decision: str, options: List[Dict[str, Any]]) -> Dict[str, Any]:
    """严格最小权限映射（借鉴 tarobird antigravity-acp-adapter.ts）：
    - 绝不将 allow_once / allow_session 扩大为 ACP 的持久化 allow_always；
    - 仅当存在精确匹配的 allow_once 选项时才放行，否则回退 reject_once 或 cancelled；
    - deny 仅匹配 reject_once（绝不扩大为 reject_always），无则 cancelled。
    """
    def _find_opt(kind: str) -> Optional[str]:
        for opt in options:
            if isinstance(opt, dict) and opt.get("kind") == kind and opt.get("optionId"):
                return str(opt["optionId"])
        return None

    reject_once = _find_opt("reject_once")
    if decision in ("allow_once", "allow_session"):
        allow_once = _find_opt("allow_once")
        if allow_once:
            return {"outcome": {"outcome": "selected", "optionId": allow_once}}
        return {"outcome": {"outcome": "selected", "optionId": reject_once}} if reject_once else {"outcome": {"outcome": "cancelled"}}

    return {"outcome": {"outcome": "selected", "optionId": reject_once}} if reject_once else {"outcome": {"outcome": "cancelled"}}


def parse_agy_json_line(line: str) -> Optional[Dict[str, Any]]:
    text = line.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def unwrap_agy_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """兼容 agy 1.5+ 外层信封格式: {"event":"step_update","step_update":{...}} / {"event":"result","result":{...}} / {"event":"init","init":{...}}。"""
    inner_event = str(ev.get("event") or "")
    if inner_event and isinstance(ev.get(inner_event), dict):
        return ev[inner_event]
    for key in ("step_update", "result", "init"):
        if isinstance(ev.get(key), dict):
            return ev[key]
    return ev


class AgyCliAdapter(BaseCliAdapter):
    """AGY CLI Phase 1 流式 JSONL 适配器。"""

    def __init__(
        self,
        *,
        executable: str = "agy",
        env: Dict[str, str],
        inactivity_timeout_sec: float = 1800.0,
        turn_timeout_sec: float = 7200.0,
        prefer_acp: bool = False,
    ):
        self.executable = executable
        self.env = env
        self.inactivity_timeout_sec = inactivity_timeout_sec
        self.turn_timeout_sec = turn_timeout_sec
        self.prefer_acp = prefer_acp
        self._active_procs: Dict[Tuple[str, str], asyncio.subprocess.Process] = {}
        self._active_rpcs: Dict[Tuple[str, str], Tuple[StdioJsonRpcClient, Optional[str]]] = {}
        self._cancelled: set[Tuple[str, str]] = set()

    def build_acp_args(self, *, cwd: str, approval_mode: str) -> List[str]:
        cli_args = ["acp"]
        if approval_mode == "auto":
            cli_args.append("--dangerously-skip-permissions")
        if cwd:
            cli_args.extend(["--add-dir", cwd])
        return build_spawn_argv(self.executable, cli_args, self.env)

    def build_args(
        self,
        *,
        cwd: str = "",
        native_session_id: Optional[str] = None,
        approval_mode: str = "auto",
        model: Optional[str] = None,
    ) -> List[str]:
        """借鉴 tarobird antigravity-adapter.ts：
        - 使用 `--input-format text --output-format stream-json`，prompt 通过 stdin 传入，避免 Windows 参数长度与转义问题；
        - 会话续接参数为 `--conversation <uuid>`；
        - 通过 `--add-dir <cwd>` 授权工作目录。
        """
        cli_args = ["--input-format", "text", "--output-format", "stream-json"]
        if approval_mode == "auto":
            cli_args.append("--dangerously-skip-permissions")
        if model:
            cli_args.extend(["--model", model])
        if native_session_id and _AGY_CONV_UUID_RE.match(native_session_id.strip()):
            cli_args.extend(["--conversation", native_session_id.strip()])
        if cwd:
            cli_args.extend(["--add-dir", cwd])
        return build_spawn_argv(self.executable, cli_args, self.env)

    async def cancel_turn(self, bot_id: str, turn_id: str) -> None:
        key = (bot_id, turn_id)
        self._cancelled.add(key)
        rpc_info = self._active_rpcs.get(key)
        if rpc_info is not None:
            rpc, sess_id = rpc_info
            if sess_id:
                try:
                    await rpc.notify("session/cancel", {"sessionId": sess_id})
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
        if approval_mode == "ask" or self.prefer_acp:
            return await self._run_acp_turn(
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
        return await self._run_stream_turn(
            bot_id=bot_id,
            turn_id=turn_id,
            cwd=cwd,
            prompt=prompt,
            native_session_id=native_session_id,
            approval_mode=approval_mode,
            model=model,
            on_event=on_event,
        )

    async def _run_acp_turn(
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
        """Phase 3: 基于 `agy acp` (ACP over stdio JSON-RPC)，支持交互式权限审批与优雅中断。"""
        key = (bot_id, turn_id)
        self._cancelled.discard(key)
        try:
            args = self.build_acp_args(cwd=cwd, approval_mode=approval_mode)
        except Exception as e:
            return TurnResult(status="failed", native_session_id=native_session_id, error=str(e))
        kwargs = build_subprocess_kwargs(cwd=cwd, env=self.env)
        try:
            proc = await asyncio.create_subprocess_exec(*args, **kwargs)
        except Exception as e:
            return TurnResult(status="failed", native_session_id=native_session_id, error=f"启动 agy acp 失败: {e}")

        self._active_procs[key] = proc
        session_id = native_session_id
        streamed_parts: List[str] = []

        async def _on_notification(method: str, params: Dict[str, Any]) -> None:
            if method != "session/update":
                return
            update = params.get("update") if isinstance(params.get("update"), dict) else params
            utype = str(update.get("sessionUpdate") or update.get("type") or "")
            if utype == "agent_message_chunk":
                content = update.get("content") if isinstance(update.get("content"), dict) else {}
                text = str(content.get("text") or update.get("text") or "")
                if text:
                    streamed_parts.append(text)
                    await on_event("text_delta", {"delta": text})
            elif utype == "agent_thought_chunk":
                content = update.get("content") if isinstance(update.get("content"), dict) else {}
                text = str(content.get("text") or update.get("text") or "")
                if text:
                    await on_event("thinking", {"text": truncate_text(text)})
            elif utype == "tool_call":
                await on_event("tool_start", {
                    "tool_id": str(update.get("toolCallId") or ""),
                    "tool_name": str(update.get("title") or update.get("kind") or "tool"),
                    "command": truncate_text(update.get("rawInput") or "", 2000),
                })
            elif utype == "tool_call_update":
                st = str(update.get("status") or "")
                if st in ("completed", "failed"):
                    await on_event("tool_end", {
                        "tool_id": str(update.get("toolCallId") or ""),
                        "tool_name": str(update.get("title") or "tool"),
                        "output": truncate_text(update.get("rawOutput") or update.get("content") or ""),
                        "is_error": st == "failed",
                    })

        async def _on_server_request(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            if method == "session/request_permission":
                options = params.get("options") if isinstance(params.get("options"), list) else []
                if approval_mode != "ask" or on_approval is None:
                    decision = "allow_once" if approval_mode == "auto" else "deny"
                else:
                    tc = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
                    tool_name = str(tc.get("title") or tc.get("kind") or "tool")
                    summary = truncate_text(tc.get("rawInput") or tool_name, 500)
                    decision = await on_approval({
                        "tool_name": tool_name,
                        "summary": summary,
                        "raw_request": params,
                    })
                return select_acp_permission_outcome(decision, options)
            return {}

        rpc = StdioJsonRpcClient(proc, on_notification=_on_notification, on_server_request=_on_server_request)
        rpc.start()
        try:
            async def _execute_acp() -> Any:
                nonlocal session_id
                await rpc.call(
                    "initialize",
                    {"protocolVersion": 1, "clientCapabilities": {}},
                    timeout=30.0,
                )
                if session_id and _AGY_CONV_UUID_RE.match(session_id.strip()):
                    try:
                        await rpc.call("session/load", {"sessionId": session_id, "cwd": cwd, "mcpServers": []}, timeout=30.0)
                    except Exception:
                        created = await rpc.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=30.0)
                        if isinstance(created, dict) and created.get("sessionId"):
                            session_id = str(created["sessionId"])
                else:
                    created = await rpc.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=30.0)
                    if isinstance(created, dict) and created.get("sessionId"):
                        session_id = str(created["sessionId"])

                self._active_rpcs[key] = (rpc, session_id)
                return await rpc.call(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
                    timeout=self.turn_timeout_sec,
                )

            prompt_res = await asyncio.wait_for(_execute_acp(), timeout=self.turn_timeout_sec)
            answer = "".join(streamed_parts)
            if answer:
                await on_event("assistant_message", {"text": truncate_text(answer)})

            stop_reason = prompt_res.get("stopReason") if isinstance(prompt_res, dict) else ""
            if key in self._cancelled or stop_reason == "cancelled":
                return TurnResult(status="cancelled", native_session_id=session_id, final_text=answer)
            return TurnResult(status="completed", native_session_id=session_id, final_text=answer)
        except asyncio.TimeoutError:
            await kill_process_tree(proc)
            return TurnResult(status="failed", native_session_id=session_id, error="agy acp 执行超时已强制终止")
        except Exception as e:
            if key in self._cancelled:
                return TurnResult(status="cancelled", native_session_id=session_id)
            return TurnResult(status="failed", native_session_id=session_id, error=f"agy acp 异常: {e}")
        finally:
            self._active_rpcs.pop(key, None)
            self._active_procs.pop(key, None)
            self._cancelled.discard(key)
            await rpc.stop()
            await kill_process_tree(proc)

    async def _run_stream_turn(
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
            cwd=cwd,
            native_session_id=native_session_id,
            approval_mode=approval_mode,
            model=model,
        )
        kwargs = build_subprocess_kwargs(cwd=cwd, env=self.env)
        try:
            proc = await asyncio.create_subprocess_exec(*args, **kwargs)
        except Exception as e:
            return TurnResult(status="failed", native_session_id=native_session_id, error=f"启动 agy 失败: {e}")

        self._active_procs[key] = proc
        conv_id = native_session_id
        streamed_parts: List[str] = []
        final_response: str = ""
        finished_error: Optional[str] = None
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
                nonlocal conv_id, final_response, finished_error
                if proc.stdin:
                    proc.stdin.write((prompt + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()

                assert proc.stdout is not None
                while True:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=self.inactivity_timeout_sec)
                    if not raw:
                        break
                    ev = parse_agy_json_line(raw.decode("utf-8", errors="replace"))
                    if not ev:
                        continue
                    body = unwrap_agy_event(ev)

                    if body.get("conversation_id") or ev.get("conversation_id"):
                        conv_id = str(body.get("conversation_id") or ev.get("conversation_id"))

                    step_type = str(body.get("step_type") or "")
                    state = str(body.get("state") or "")

                    if body.get("text_delta"):
                        delta = str(body["text_delta"])
                        streamed_parts.append(delta)
                        await on_event("text_delta", {"delta": delta})

                    if body.get("tool_name") or isinstance(body.get("tool_info"), dict):
                        tinfo = body.get("tool_info") if isinstance(body.get("tool_info"), dict) else {}
                        tname = str(body.get("tool_name") or tinfo.get("name") or "tool")
                        tid = str(body.get("step_index") or "")
                        if state == "ACTIVE":
                            await on_event("tool_start", {
                                "tool_id": tid,
                                "tool_name": tname,
                                "parameters": truncate_text(tinfo.get("parameters") or "", 2000),
                            })
                        elif state in ("DONE", "ERROR"):
                            await on_event("tool_end", {
                                "tool_id": tid,
                                "tool_name": tname,
                                "output": truncate_text(tinfo.get("output") or tinfo.get("error") or ""),
                                "is_error": state == "ERROR" or bool(tinfo.get("error")),
                            })

                    if step_type == "thinking" and body.get("text"):
                        await on_event("thinking", {"text": truncate_text(body["text"])})

                    # 终态回执校验（借鉴 tarobird：优先以 finished_reason 为准）
                    if "finished_reason" in body or "response" in body:
                        if body.get("finished_reason") == "error":
                            finished_error = truncate_text(body.get("error") or "AGY returned error", 1000)
                            await on_event("error", {"message": finished_error})
                        if isinstance(body.get("response"), str) and body["response"].strip():
                            final_response = body["response"]

                rc_inner = await proc.wait()
                try:
                    await asyncio.wait_for(stderr_task, timeout=2.0)
                except asyncio.TimeoutError:
                    stderr_task.cancel()
                return rc_inner

            rc = await asyncio.wait_for(_execute_full_turn(), timeout=self.turn_timeout_sec)

            answer = final_response or "".join(streamed_parts)
            if answer and not final_response:
                await on_event("assistant_message", {"text": truncate_text(answer)})
            elif final_response:
                await on_event("assistant_message", {"text": truncate_text(final_response)})

            if key in self._cancelled:
                return TurnResult(status="cancelled", native_session_id=conv_id, final_text=answer)
            if finished_error:
                return TurnResult(status="failed", native_session_id=conv_id, final_text=answer, error=finished_error)
            if rc != 0:
                diag = "".join(stderr_chunks).strip()[-1000:]
                return TurnResult(
                    status="failed",
                    native_session_id=conv_id,
                    final_text=answer,
                    error=f"agy 退出码 {rc}" + (f": {diag}" if diag else ""),
                )
            return TurnResult(status="completed", native_session_id=conv_id, final_text=answer)
        except asyncio.TimeoutError:
            await kill_process_tree(proc)
            return TurnResult(status="failed", native_session_id=conv_id, error="agy 执行超时已强制终止")
        finally:
            self._active_procs.pop(key, None)
            self._cancelled.discard(key)
            if not stderr_task.done():
                stderr_task.cancel()

"""
PC CLI 中枢协调核心 (features.pc_cli.hub).
负责维护在线 PC Daemon 会话、手机客户端订阅、Bot 生命周期、回合派发、事件落盘与广播、审批闭环。
"""

from __future__ import annotations

import logging
import posixpath
import ntpath
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from lite_agent.features.pc_cli.contracts import (
    APPROVAL_MODES,
    ApprovalDecision,
    ApprovalType,
    BotStatus,
    EventType,
    NodeStatus,
    SUPPORTED_ENGINES,
    TurnStatus,
    VALID_DECISIONS,
    is_valid_model_name,
)
from lite_agent.features.pc_cli.repository import PcCliRepository

logger = logging.getLogger("features.pc_cli.hub")

SendFunc = Callable[[Dict[str, Any]], None]


class PcCliHub:
    """PC CLI 中枢业务核心。"""

    def __init__(
        self,
        repo: PcCliRepository,
        notifier: Optional[Callable[[str, str], None]] = None,
    ):
        self.repo = repo
        self.notifier = notifier
        self._lock = threading.RLock()
        # node_id -> (conn_id, send_fn)
        self._daemons: Dict[str, Tuple[str, SendFunc]] = {}
        # client_id -> (subscribed_bot_id_or_none, send_fn)
        self._clients: Dict[str, Tuple[Optional[str], SendFunc]] = {}
        # req_id -> (Event, response_holder)
        self._dir_waiters: Dict[str, Tuple[threading.Event, Dict[str, Any]]] = {}
        # approval_id -> (Event, ack_holder)
        self._approval_ack_waiters: Dict[str, Tuple[threading.Event, Dict[str, Any]]] = {}
        # 启动时重置所有节点状态为离线，等待真实 Daemon 重连上报
        self.repo.mark_all_nodes_offline()

    def close(self) -> None:
        with self._lock:
            self._daemons.clear()
            self._clients.clear()
            for ev, holder in self._dir_waiters.values():
                holder["error"] = "hub_closing"
                ev.set()
            self._dir_waiters.clear()
            for ev, holder in self._approval_ack_waiters.values():
                holder["accepted"] = False
                holder["reason"] = "hub_closing"
                ev.set()
            self._approval_ack_waiters.clear()
        self.repo.close()

    # ------------------------------------------------------------------
    # Client Subscriptions (Mobile PWA)
    # ------------------------------------------------------------------
    def register_client(self, client_id: str, send_fn: SendFunc, bot_id: Optional[str] = None) -> None:
        with self._lock:
            self._clients[client_id] = (bot_id, send_fn)

    def update_client_subscription(self, client_id: str, bot_id: Optional[str]) -> None:
        with self._lock:
            if client_id in self._clients:
                _, fn = self._clients[client_id]
                self._clients[client_id] = (bot_id, fn)

    def unregister_client(self, client_id: str) -> None:
        with self._lock:
            self._clients.pop(client_id, None)

    def _broadcast_clients(self, message: Dict[str, Any], bot_id_filter: Optional[str] = None) -> None:
        with self._lock:
            targets = list(self._clients.items())
        for cid, (sub_bot, fn) in targets:
            if bot_id_filter is not None and sub_bot != bot_id_filter:
                continue
            try:
                fn(message)
            except Exception as e:
                logger.debug(f"向客户端 {cid} 推送失败: {e}")

    def _send_to_daemon(self, node_id: str, message: Dict[str, Any]) -> bool:
        with self._lock:
            entry = self._daemons.get(node_id)
        if not entry:
            return False
        _, send_fn = entry
        try:
            send_fn(message)
            return True
        except Exception as e:
            logger.warning(f"向节点 {node_id} 发送消息失败: {e}")
            return False

    def is_node_online(self, node_id: str) -> bool:
        with self._lock:
            return node_id in self._daemons

    # ------------------------------------------------------------------
    # Daemon Lifecycle Handlers
    # ------------------------------------------------------------------
    def handle_daemon_hello(self, conn_id: str, send_fn: SendFunc, payload: Dict[str, Any]) -> Dict[str, Any]:
        node_id = str(payload.get("node_id") or "").strip()
        if not node_id:
            raise ValueError("node_id 不能为空")
        display_name = str(payload.get("display_name") or node_id).strip()
        platform = str(payload.get("platform") or "unknown").strip()
        engines = [str(e) for e in (payload.get("engines") or []) if str(e) in SUPPORTED_ENGINES]
        workspace_roots = [str(p) for p in (payload.get("workspace_roots") or []) if str(p).strip()]

        with self._lock:
            self._daemons[node_id] = (conn_id, send_fn)

        node = self.repo.upsert_node(
            node_id=node_id,
            display_name=display_name,
            platform=platform,
            engines=engines,
            workspace_roots=workspace_roots,
            status=NodeStatus.ONLINE.value,
        )
        self._broadcast_clients({"type": "nodes.updated", "nodes": self.repo.list_nodes()})
        logger.info(f"PC 节点上线: {node_id} ({platform}, engines={engines})")
        return node

    def handle_daemon_disconnect(self, node_id: str, conn_id: str) -> None:
        removed = False
        with self._lock:
            entry = self._daemons.get(node_id)
            if entry and entry[0] == conn_id:
                self._daemons.pop(node_id, None)
                removed = True
        if removed:
            self.repo.mark_node_offline(node_id)
            expired_cnt = self.repo.expire_pending_approvals(node_id=node_id)
            if expired_cnt:
                logger.info(f"节点 {node_id} 断开，已自动失效 {expired_cnt} 条挂起审批单")
            self._broadcast_clients({"type": "nodes.updated", "nodes": self.repo.list_nodes()})
            logger.info(f"PC 节点下线: {node_id}")

    def _is_active_daemon_conn(self, node_id: str, conn_id: Optional[str]) -> bool:
        with self._lock:
            entry = self._daemons.get(node_id)
        if not entry:
            return False
        if conn_id is not None and entry[0] != conn_id:
            return False
        return True

    def _validate_daemon_turn_ownership(
        self,
        *,
        sender_node_id: Optional[str],
        conn_id: Optional[str],
        bot_id: str,
        turn_id: str,
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """强校验：发送连接仍活跃 + Bot 存在且归属该节点 + Turn 存在且未终结。"""
        if not bot_id or not turn_id or not sender_node_id:
            logger.warning(f"拒绝缺少归属字段的 Daemon 回合消息: node={sender_node_id} bot={bot_id} turn={turn_id}")
            return None
        if not self._is_active_daemon_conn(sender_node_id, conn_id):
            logger.warning(f"拒绝来自旧/非活跃连接的 Daemon 消息: node={sender_node_id} conn={conn_id}")
            return None
        bot = self.repo.get_bot(bot_id)
        if not bot:
            logger.info(f"忽略已删除 Bot 的迟到消息: bot={bot_id} turn={turn_id}")
            return None
        if bot["node_id"] != sender_node_id:
            logger.warning(f"拒绝跨节点越权消息: sender={sender_node_id} bot_owner={bot['node_id']} bot={bot_id}")
            return None
        turn = self.repo.get_turn(turn_id)
        if not turn or turn["bot_id"] != bot_id:
            logger.warning(f"拒绝不存在或不匹配的 Turn 消息: bot={bot_id} turn={turn_id}")
            return None
        if turn["status"] not in (TurnStatus.QUEUED.value, TurnStatus.RUNNING.value):
            logger.info(f"忽略已终结 Turn ({turn['status']}) 的迟到消息: bot={bot_id} turn={turn_id}")
            return None
        return bot, turn

    def handle_daemon_heartbeat(self, node_id: str, payload: Dict[str, Any], conn_id: Optional[str] = None) -> None:
        if not self._is_active_daemon_conn(node_id, conn_id):
            return
        self.repo.touch_node_heartbeat(node_id)

    def handle_daemon_dir_list_result(
        self,
        payload: Dict[str, Any],
        sender_node_id: Optional[str] = None,
        conn_id: Optional[str] = None,
    ) -> None:
        req_id = str(payload.get("req_id") or "")
        with self._lock:
            waiter = self._dir_waiters.get(req_id)
        if not waiter:
            return
        ev, holder = waiter
        expected_node = holder.get("_expected_node_id")
        if sender_node_id and expected_node and sender_node_id != expected_node:
            logger.warning(f"拒绝跨节点 dir.list_result: sender={sender_node_id} expected={expected_node}")
            return
        if sender_node_id and not self._is_active_daemon_conn(sender_node_id, conn_id):
            return
        holder["entries"] = payload.get("entries") or []
        if payload.get("error"):
            holder["error"] = str(payload["error"])
        ev.set()

    def handle_daemon_event(
        self,
        payload: Dict[str, Any],
        sender_node_id: Optional[str] = None,
        conn_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        bot_id = str(payload.get("bot_id") or "")
        turn_id = str(payload.get("turn_id") or "")
        event_type = str(payload.get("event_type") or "")
        event_payload = payload.get("payload") or {}
        if not event_type:
            return None
        if not self._validate_daemon_turn_ownership(
            sender_node_id=sender_node_id,
            conn_id=conn_id,
            bot_id=bot_id,
            turn_id=turn_id,
        ):
            return None

        ev = self.repo.append_event(
            bot_id=bot_id,
            turn_id=turn_id,
            event_type=event_type,
            payload=event_payload,
        )
        # 提取可读进度摘要更新 Bot 卡片
        summary = self._extract_summary(event_type, event_payload)
        if summary is not None:
            self.repo.set_bot_status(bot_id, BotStatus.RUNNING.value, last_summary=summary)
            self._emit_bot_summary(bot_id)

        self._broadcast_clients({"type": "bot.event", "event": ev}, bot_id_filter=bot_id)
        return ev

    def handle_daemon_approval_request(
        self,
        payload: Dict[str, Any],
        sender_node_id: Optional[str] = None,
        conn_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        bot_id = str(payload.get("bot_id") or "")
        turn_id = str(payload.get("turn_id") or "")
        approval_id = str(payload.get("approval_id") or "") or None
        tool_name = str(payload.get("tool_name") or "tool")
        summary = str(payload.get("summary") or f"请求执行 {tool_name}")
        raw_req = payload.get("raw_request") or {}
        if not self._validate_daemon_turn_ownership(
            sender_node_id=sender_node_id,
            conn_id=conn_id,
            bot_id=bot_id,
            turn_id=turn_id,
        ):
            return None

        appr = self.repo.create_approval(
            approval_id=approval_id,
            bot_id=bot_id,
            turn_id=turn_id,
            tool_name=tool_name,
            summary=summary,
            raw_request=raw_req,
            approval_type=ApprovalType.CLI_TOOL.value,
            requested_by="daemon",
        )
        ev = self.repo.append_event(
            bot_id=bot_id,
            turn_id=turn_id,
            event_type=EventType.APPROVAL_REQUEST.value,
            payload=appr,
        )
        self.repo.set_bot_status(bot_id, BotStatus.WAITING_APPROVAL.value, last_summary=f"等待审批: {summary}")
        self._emit_bot_summary(bot_id)
        self._broadcast_clients({"type": "bot.event", "event": ev}, bot_id_filter=bot_id)
        self._notify(f"PC CLI 待审批 ({bot_id})", summary, kind="approval", bot_id=bot_id)
        return appr

    def handle_daemon_approval_ack(
        self,
        payload: Dict[str, Any],
        sender_node_id: Optional[str] = None,
        conn_id: Optional[str] = None,
    ) -> None:
        approval_id = str(payload.get("approval_id") or "")
        bot_id = str(payload.get("bot_id") or "")
        turn_id = str(payload.get("turn_id") or "")
        with self._lock:
            waiter = self._approval_ack_waiters.get(approval_id)
        if not waiter:
            return
        ev, holder = waiter
        if sender_node_id and holder.get("_expected_node_id") != sender_node_id:
            return
        if sender_node_id and not self._is_active_daemon_conn(sender_node_id, conn_id):
            return
        if holder.get("_expected_bot_id") and bot_id and holder["_expected_bot_id"] != bot_id:
            return
        if holder.get("_expected_turn_id") and turn_id and holder["_expected_turn_id"] != turn_id:
            return
        holder["accepted"] = bool(payload.get("accepted"))
        if payload.get("reason"):
            holder["reason"] = str(payload["reason"])
        ev.set()

    def handle_daemon_turn_finished(
        self,
        payload: Dict[str, Any],
        sender_node_id: Optional[str] = None,
        conn_id: Optional[str] = None,
    ) -> None:
        bot_id = str(payload.get("bot_id") or "")
        turn_id = str(payload.get("turn_id") or "")
        status = str(payload.get("status") or TurnStatus.COMPLETED.value)
        native_session_id = payload.get("native_session_id")
        error_msg = payload.get("error")
        owned = self._validate_daemon_turn_ownership(
            sender_node_id=sender_node_id,
            conn_id=conn_id,
            bot_id=bot_id,
            turn_id=turn_id,
        )
        if not owned:
            return
        bot, _turn = owned

        self.repo.finish_turn(turn_id, status=status, error_message=error_msg)
        self.repo.expire_pending_approvals(turn_id=turn_id)
        bot_status = BotStatus.ERROR.value if status == TurnStatus.FAILED.value else BotStatus.IDLE.value
        summary = (
            f"失败: {error_msg}" if status == TurnStatus.FAILED.value and error_msg
            else ("已中止" if status == TurnStatus.CANCELLED.value else None)
        )
        self.repo.set_bot_status(
            bot_id,
            bot_status,
            last_summary=summary,
            native_session_id=str(native_session_id) if native_session_id else None,
        )
        ev = self.repo.append_event(
            bot_id=bot_id,
            turn_id=turn_id,
            event_type=EventType.TURN_STATUS.value,
            payload={"status": status, "error": error_msg, "native_session_id": native_session_id},
        )
        self._emit_bot_summary(bot_id)
        self._broadcast_clients({"type": "bot.event", "event": ev}, bot_id_filter=bot_id)
        if status in (TurnStatus.COMPLETED.value, TurnStatus.FAILED.value):
            bname = bot["name"] if bot else bot_id
            latest = self.repo.get_bot(bot_id)
            self._notify(
                f"Bot [{bname}] {status}",
                error_msg or (latest["last_summary"] if latest else ""),
                kind="complete",
                bot_id=bot_id,
            )

    # ------------------------------------------------------------------
    # Public Business Operations (REST / Manager)
    # ------------------------------------------------------------------
    def list_nodes(self) -> List[Dict[str, Any]]:
        return self.repo.list_nodes()

    def list_node_dirs(self, node_id: str, path: str = "", timeout: float = 5.0) -> Tuple[bool, Dict[str, Any]]:
        if not self.is_node_online(node_id):
            return False, {"error": f"节点 '{node_id}' 当前不在线"}
        req_id = f"dir_{uuid.uuid4().hex[:10]}"
        ev = threading.Event()
        holder: Dict[str, Any] = {"_expected_node_id": node_id}
        with self._lock:
            self._dir_waiters[req_id] = (ev, holder)
        try:
            sent = self._send_to_daemon(node_id, {"type": "dir.list_request", "req_id": req_id, "path": path})
            if not sent:
                return False, {"error": f"向节点 '{node_id}' 发送目录查询失败"}
            if not ev.wait(timeout=timeout):
                return False, {"error": "查询 PC 目录超时"}
            if holder.get("error"):
                return False, {"error": holder["error"]}
            return True, {"node_id": node_id, "path": path, "entries": holder.get("entries", [])}
        finally:
            with self._lock:
                self._dir_waiters.pop(req_id, None)

    def create_bot(
        self,
        *,
        name: str,
        node_id: str,
        engine: str,
        cwd: str,
        approval_mode: str = "auto",
        model: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        name = (name or "").strip()
        node_id = (node_id or "").strip()
        engine = (engine or "").strip().lower()
        cwd = (cwd or "").strip()
        approval_mode = (approval_mode or "auto").strip().lower()

        if not name:
            return False, {"error": "Bot 名称不能为空"}
        if engine not in SUPPORTED_ENGINES:
            return False, {"error": f"不支持的引擎 '{engine}'，仅支持 {SUPPORTED_ENGINES}"}
        if approval_mode not in APPROVAL_MODES:
            return False, {"error": f"不支持的审批模式 '{approval_mode}'，仅支持 {APPROVAL_MODES}"}
        if not cwd:
            return False, {"error": "工作目录 cwd 不能为空"}
        clean_model = (model or "").strip() or None
        if not is_valid_model_name(clean_model):
            return False, {"error": f"无效的模型名 '{clean_model}'，仅允许字母、数字及 . _ : / -"}

        node = self.repo.get_node(node_id)
        if not node:
            return False, {"error": f"节点 '{node_id}' 不存在，请先启动该 PC 的守护进程"}
        if node.get("engines") and engine not in node["engines"]:
            return False, {"error": f"节点 '{node_id}' 未检测到引擎 '{engine}' (可用: {node['engines']})"}
        roots = node.get("workspace_roots") or []
        if roots and not self._is_cwd_in_roots(cwd, roots, platform=str(node.get("platform") or "")):
            return False, {"error": f"工作目录 '{cwd}' 不在节点 '{node_id}' 允许的白名单根目录内: {roots}"}

        bot = self.repo.create_bot(
            name=name,
            node_id=node_id,
            engine=engine,
            cwd=cwd,
            approval_mode=approval_mode,
            model=clean_model,
        )
        self._emit_bot_summary(bot["bot_id"])
        return True, {"bot": bot}

    def list_bots(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        return self.repo.list_bots(include_archived=include_archived)

    def update_bot(self, bot_id: str, **kwargs: Any) -> Tuple[bool, Dict[str, Any]]:
        if "approval_mode" in kwargs and kwargs["approval_mode"] is not None:
            if kwargs["approval_mode"] not in APPROVAL_MODES:
                return False, {"error": f"不支持的审批模式: {kwargs['approval_mode']}"}
        if "model" in kwargs and kwargs["model"] is not None:
            clean_model = str(kwargs["model"]).strip()
            if not is_valid_model_name(clean_model):
                return False, {"error": f"无效的模型名: {clean_model}"}
            kwargs["model"] = clean_model
        bot = self.repo.update_bot(bot_id, **kwargs)
        if not bot:
            return False, {"error": f"Bot '{bot_id}' 不存在"}
        self._emit_bot_summary(bot_id)
        return True, {"bot": bot}

    def delete_bot(self, bot_id: str) -> Tuple[bool, Dict[str, Any]]:
        bot = self.repo.get_bot(bot_id)
        if not bot:
            return False, {"error": f"Bot '{bot_id}' 不存在"}
        # 若有活跃回合，先尽力通知 Daemon 中止
        active = self.repo.get_active_turn_for_bot(bot_id)
        if active and self.is_node_online(bot["node_id"]):
            self._send_to_daemon(bot["node_id"], {
                "type": "turn.cancel",
                "bot_id": bot_id,
                "turn_id": active["turn_id"],
            })
        self.repo.delete_bot(bot_id)
        self._broadcast_clients({"type": "bot.deleted", "bot_id": bot_id})
        return True, {"deleted": True, "bot_id": bot_id}

    def start_turn(
        self,
        bot_id: str,
        prompt: str,
        input_source: str = "text",
    ) -> Tuple[bool, Dict[str, Any]]:
        prompt = (prompt or "").strip()
        if not prompt:
            return False, {"error": "指令内容 prompt 不能为空"}
        with self._lock:
            bot = self.repo.get_bot(bot_id)
            if not bot:
                return False, {"error": f"Bot '{bot_id}' 不存在"}
            if not self.is_node_online(bot["node_id"]):
                return False, {"error": f"目标 PC 节点 '{bot['node_id']}' 当前不在线"}
            active = self.repo.get_active_turn_for_bot(bot_id)
            if active:
                return False, {"error": f"Bot 当前已有运行中的回合 ({active['turn_id']})，请等待完成或先中止"}

            try:
                turn, ev = self.repo.start_turn_atomic(
                    bot_id=bot_id,
                    prompt=prompt,
                    input_source=input_source,
                    summary=f"指令: {prompt[:80]}",
                )
            except Exception as e:
                return False, {"error": f"创建回合冲突（可能已有并发运行中的回合）: {e}"}

        sent = self._send_to_daemon(
            bot["node_id"],
            {
                "type": "turn.start",
                "bot_id": bot_id,
                "turn_id": turn["turn_id"],
                "engine": bot["engine"],
                "cwd": bot["cwd"],
                "prompt": prompt,
                "native_session_id": bot.get("native_session_id"),
                "approval_mode": bot["approval_mode"],
                "model": bot.get("model"),
            },
        )
        if not sent:
            self.repo.finish_turn(turn["turn_id"], TurnStatus.FAILED.value, error_message="派发到 PC 节点失败")
            self.repo.set_bot_status(bot_id, BotStatus.ERROR.value, last_summary="派发到 PC 节点失败")
            return False, {"error": "派发指令到 PC 节点失败"}

        self._emit_bot_summary(bot_id)
        self._broadcast_clients({"type": "bot.event", "event": ev}, bot_id_filter=bot_id)
        return True, {"turn": turn, "event": ev}

    def cancel_turn(self, bot_id: str, expected_turn_id: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
        bot = self.repo.get_bot(bot_id)
        if not bot:
            return False, {"error": f"Bot '{bot_id}' 不存在"}
        active = self.repo.get_active_turn_for_bot(bot_id)
        if not active:
            return False, {"error": "当前没有运行中的回合"}
        if expected_turn_id and active["turn_id"] != expected_turn_id:
            return False, {"error": f"目标回合 '{expected_turn_id}' 已结束，拒绝误中止新回合 ({active['turn_id']})"}
        self.repo.expire_pending_approvals(turn_id=active["turn_id"])
        sent = self._send_to_daemon(
            bot["node_id"],
            {"type": "turn.cancel", "bot_id": bot_id, "turn_id": active["turn_id"]},
        )
        if not sent:
            self.repo.finish_turn(active["turn_id"], TurnStatus.CANCELLED.value, error_message="节点离线，本地强制标记中止")
            self.repo.set_bot_status(bot_id, BotStatus.IDLE.value, last_summary="已强制中止（未获 PC 确认）")
            self._emit_bot_summary(bot_id)
            return True, {
                "cancel_state": "hub_marked_cancelled",
                "cancel_requested": False,
                "delivered": False,
                "hub_marked_cancelled": True,
                "daemon_confirmed": False,
                "cancelled": True,
                "turn_id": active["turn_id"],
                "forced": True,
            }
        return True, {
            "cancel_state": "cancel_requested",
            "cancel_requested": True,
            "delivered": True,
            "hub_marked_cancelled": False,
            "daemon_confirmed": False,
            "cancelled": False,
            "turn_id": active["turn_id"],
            "forced": False,
        }

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        input_source: str = "button",
    ) -> Tuple[bool, Dict[str, Any]]:
        if decision not in VALID_DECISIONS:
            return False, {"error": f"无效的审批决策 '{decision}'，仅支持 {VALID_DECISIONS}"}
        existing = self.repo.get_approval(approval_id)
        if not existing:
            return False, {"error": f"审批单 '{approval_id}' 不存在"}
        if existing["decision"] != ApprovalDecision.PENDING.value:
            return False, {"error": f"审批单 '{approval_id}' 已被处理 (状态: {existing['decision']})"}

        bot_id = existing["bot_id"]
        turn_id = existing.get("turn_id") or ""
        atype = existing["approval_type"]

        # cli_tool 审批必须校验 Bot 存在、回合仍活跃、且目标 PC 在线；否则直接失效拒绝
        if atype == ApprovalType.CLI_TOOL.value:
            bot = self.repo.get_bot(bot_id)
            turn = self.repo.get_turn(turn_id) if turn_id else None
            if (
                not bot
                or not turn
                or turn["status"] not in (TurnStatus.QUEUED.value, TurnStatus.RUNNING.value)
                or not self.is_node_online(bot["node_id"])
            ):
                expired = self.repo.mark_approval_expired(approval_id)
                return False, {
                    "error": "目标 PC 不在线或回合已结束，审批单已失效",
                    "approval": expired,
                    "delivered": False,
                }

        # turn_cancel 审批必须校验绑定的原 turn_id 仍活跃，防止误中止同一 Bot 后续新回合
        if atype == ApprovalType.TURN_CANCEL.value:
            turn = self.repo.get_turn(turn_id) if turn_id else None
            if not turn or turn["bot_id"] != bot_id or turn["status"] not in (TurnStatus.QUEUED.value, TurnStatus.RUNNING.value):
                expired = self.repo.mark_approval_expired(approval_id)
                return False, {
                    "error": "原回合已结束，中止授权单已失效",
                    "approval": expired,
                    "executed": False,
                }

        ok, appr = self.repo.resolve_approval_cas(approval_id, decision, input_source=input_source)
        if not appr:
            return False, {"error": f"审批单 '{approval_id}' 不存在"}
        if not ok:
            return False, {"error": f"审批单 '{approval_id}' 已被处理 (状态: {appr['decision']})"}

        approved = decision in (ApprovalDecision.ALLOW_ONCE.value, ApprovalDecision.ALLOW_SESSION.value)

        if atype == ApprovalType.BOT_DELETE.value:
            if not approved:
                return True, {"approval": appr, "executed": False, "delivered": True}
            ok_del, del_res = self.delete_bot(bot_id)
            if not ok_del:
                return False, {"approval": appr, "executed": False, "error": del_res.get("error", "删除 Bot 失败")}
            return True, {"approval": appr, "executed": True, "delivered": True}

        if atype == ApprovalType.TURN_CANCEL.value:
            if not approved:
                return True, {"approval": appr, "executed": False, "delivered": False, "daemon_confirmed": False}
            ok_c, c_res = self.cancel_turn(bot_id, expected_turn_id=turn_id)
            if not ok_c:
                return False, {
                    "approval": appr,
                    "executed": False,
                    "delivered": False,
                    "daemon_confirmed": False,
                    "error": c_res.get("error", "中止回合失败"),
                }
            # 严格区分中止请求已发送 / Hub 本地强制结束 / Daemon 确认停止（此时均尚未收到 Daemon 终态）
            return True, {
                "approval": appr,
                "executed": False,
                **c_res,
            }

        # 默认 cli_tool：必须等待 Daemon 显式 turn.approval_ack 确认已命中挂起 Future
        bot = self.repo.get_bot(bot_id)
        if not bot:
            expired = self.repo.mark_approval_expired(approval_id)
            return False, {"error": "Bot 不存在，审批单已失效", "approval": expired, "delivered": False}

        ack_ev = threading.Event()
        ack_holder: Dict[str, Any] = {
            "_expected_node_id": bot["node_id"],
            "_expected_bot_id": bot_id,
            "_expected_turn_id": turn_id,
            "accepted": False,
        }
        with self._lock:
            self._approval_ack_waiters[approval_id] = (ack_ev, ack_holder)
        try:
            sent = self._send_to_daemon(
                bot["node_id"],
                {
                    "type": "turn.approval_decision",
                    "bot_id": bot_id,
                    "turn_id": turn_id,
                    "approval_id": approval_id,
                    "decision": decision,
                },
            )
            if not sent:
                expired = self.repo.mark_approval_expired(approval_id)
                return False, {
                    "error": "审批决策未能送达 PC 守护进程，审批单已失效",
                    "approval": expired,
                    "delivered": False,
                }
            acked = ack_ev.wait(timeout=3.0)
            if not acked or not ack_holder.get("accepted"):
                expired = self.repo.mark_approval_expired(approval_id)
                reason = ack_holder.get("reason") or ("等待引擎审批回执超时" if not acked else "引擎未接受审批")
                return False, {
                    "error": f"审批未被引擎确认接收 ({reason})，审批单已失效",
                    "approval": expired,
                    "delivered": False,
                }
        finally:
            with self._lock:
                self._approval_ack_waiters.pop(approval_id, None)

        # 收到真实回执后，在锁内二次校验回合仍活跃再置为 running，绝不覆盖并发终态
        with self._lock:
            latest_turn = self.repo.get_turn(turn_id) if turn_id else None
            turn_still_active = bool(
                latest_turn and latest_turn["status"] in (TurnStatus.QUEUED.value, TurnStatus.RUNNING.value)
            )
            ev = self.repo.append_event(
                bot_id=bot_id,
                turn_id=turn_id,
                event_type=EventType.APPROVAL_RESOLVED.value,
                payload={"approval_id": approval_id, "decision": decision, "input_source": input_source, "delivered": True},
            )
            if turn_still_active:
                self.repo.set_bot_status(bot_id, BotStatus.RUNNING.value, last_summary=f"守护进程已接收审批: {decision}")
        self._emit_bot_summary(bot_id)
        self._broadcast_clients({"type": "bot.event", "event": ev}, bot_id_filter=bot_id)
        return True, {"approval": appr, "delivered": True}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_cwd_in_roots(cwd: str, roots: List[str], platform: str = "") -> bool:
        """按目标节点平台做规范化路径前缀校验，阻断越权目录与 `..` 穿越。"""
        is_win = platform == "win32" or (len(cwd) >= 2 and cwd[1] == ":")
        p_mod = ntpath if is_win else posixpath
        norm_cwd = p_mod.normpath(cwd)
        if ".." in norm_cwd.replace("\\", "/").split("/"):
            return False
        cmp_cwd = norm_cwd.lower() if is_win else norm_cwd
        for r in roots:
            norm_r = p_mod.normpath(r)
            cmp_r = norm_r.lower() if is_win else norm_r
            try:
                if p_mod.commonpath([cmp_cwd, cmp_r]) == cmp_r:
                    return True
            except ValueError:
                continue
        return False
    def _emit_bot_summary(self, bot_id: str) -> None:
        bots = self.repo.list_bots(include_archived=True)
        for b in bots:
            if b["bot_id"] == bot_id:
                self._broadcast_clients({"type": "bot.summary_updated", "bot": b})
                return

    @staticmethod
    def _extract_summary(event_type: str, payload: Dict[str, Any]) -> Optional[str]:
        if event_type == EventType.TOOL_START.value:
            name = payload.get("tool_name") or "tool"
            return f"执行工具: {name}"
        if event_type == EventType.ASSISTANT_MESSAGE.value:
            text = str(payload.get("text") or "").strip().replace("\n", " ")
            return text[:120] if text else None
        if event_type == EventType.ERROR.value:
            return f"错误: {str(payload.get('message') or '')[:100]}"
        return None

    def _notify(self, title: str, content: str, *, kind: str = "complete", bot_id: Optional[str] = None) -> None:
        if self.notifier:
            try:
                self.notifier(title, content, kind=kind, bot_id=bot_id)
            except TypeError:
                try:
                    self.notifier(title, content)
                except Exception as e:
                    logger.debug(f"发送提醒失败: {e}")
            except Exception as e:
                logger.debug(f"发送提醒失败: {e}")

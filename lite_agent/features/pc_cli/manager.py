"""
PC CLI LLM 总管模块 (features.pc_cli.manager).
- 复用 conf.d/llm.json 只读模型快照 (build_model_snapshot)，支持请求级模型切换，绝不修改配置文件。
- 三权分立工具集：
  1. 只读类：list_nodes / list_bots / get_bot_progress / get_dashboard_stats（直接执行）
  2. 创建/派发类：create_bot / dispatch_turn（经 Hub 白名单校验执行）
  3. 高危类：request_delete_bot / request_cancel_turn（严禁直接执行，强制生成 pending 审批单供手机确认）
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from lite_agent.core.model_management import build_model_snapshot
from lite_agent.features.pc_cli.contracts import ApprovalType
from lite_agent.features.pc_cli.hub import PcCliHub
from lite_agent.features.pc_cli.stats import collect_dashboard_stats

logger = logging.getLogger("features.pc_cli.manager")

_SYSTEM_PROMPT = (
    "你是 Lite Agent 多 PC CLI (Codex / AGY) 手机总管助理。\n"
    "职责：帮助用户查看在线 PC 节点、创建或调度 Bot、汇总执行进度与统计看板。\n"
    "安全铁律：\n"
    "1. 你没有直接删除 Bot 或强杀回合的权限；若用户要求删除 Bot 或中止任务，"
    "必须调用 request_delete_bot 或 request_cancel_turn 发起授权单，并提醒用户在手机确认。\n"
    "2. 创建 Bot 前请先调用 list_nodes 核对目标节点在线状态、可用引擎与白名单目录。\n"
    "3. 回复简洁清晰，使用中文。"
)

MANAGER_TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_nodes",
            "description": "获取所有 PC 节点（Windows/macOS）在线状态、可用引擎及白名单工作根目录。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_bots",
            "description": "获取所有已创建的 PC CLI Bot 列表、所属节点、状态与最新进度摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_archived": {"type": "boolean", "description": "是否包含已归档 Bot"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bot_progress",
            "description": "获取指定 Bot 最近若干条事件日志，用于汇总进度或定位报错。",
            "parameters": {
                "type": "object",
                "properties": {
                    "bot_id": {"type": "string", "description": "Bot ID"},
                    "limit": {"type": "integer", "description": "返回最近条数，默认 20"},
                },
                "required": ["bot_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dashboard_stats",
            "description": "获取总体看板统计（节点在线数、Bot 状态分布、近 N 天回合成功率、待审批数与总管消耗）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "统计天数，默认 7"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_bot",
            "description": "在指定在线 PC 上创建一个新的 Codex 或 AGY Bot。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Bot 名称"},
                    "node_id": {"type": "string", "description": "目标 PC 节点 ID"},
                    "engine": {"type": "string", "enum": ["codex", "agy"], "description": "CLI 引擎"},
                    "cwd": {"type": "string", "description": "目标 PC 工作目录绝对路径"},
                    "approval_mode": {"type": "string", "enum": ["auto", "ask"], "description": "权限模式，默认 auto"},
                    "model": {"type": "string", "description": "可选 CLI 模型覆盖"},
                },
                "required": ["name", "node_id", "engine", "cwd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_turn",
            "description": "向指定空闲 Bot 下发一条执行指令。",
            "parameters": {
                "type": "object",
                "properties": {
                    "bot_id": {"type": "string", "description": "目标 Bot ID"},
                    "prompt": {"type": "string", "description": "下发给 CLI 的指令文本"},
                },
                "required": ["bot_id", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_delete_bot",
            "description": "【高危授权】发起删除指定 Bot 的审批单（不会直接删除，需用户在手机确认后执行）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "bot_id": {"type": "string", "description": "拟删除的 Bot ID"},
                    "reason": {"type": "string", "description": "删除原因说明"},
                },
                "required": ["bot_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_cancel_turn",
            "description": "【高危授权】发起中止指定 Bot 当前运行回合的审批单（需用户在手机确认后执行）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "bot_id": {"type": "string", "description": "目标 Bot ID"},
                    "reason": {"type": "string", "description": "中止原因说明"},
                },
                "required": ["bot_id", "reason"],
            },
        },
    },
]


class PcCliManager:
    """复用 conf.d/llm.json 模型切换与受控三权分立工具集的 AI 总管。"""

    def __init__(
        self,
        *,
        hub: PcCliHub,
        config: Dict[str, Any],
        gateway: Optional[Any] = None,
        default_model: str = "",
    ):
        self.hub = hub
        self.config = config or {}
        self.gateway = gateway
        self.default_model = default_model

    def list_models(self) -> Dict[str, Any]:
        """返回 conf.d/llm.json 脱敏模型目录快照（只读，不含 API Key）。"""
        snap = build_model_snapshot(self.config)
        models = snap.get("models") or []
        defaults = snap.get("defaults") or {}
        default_name = self.default_model or defaults.get("default_model") or (models[0]["name"] if models else "")
        return {
            "models": models,
            "default_model": default_name,
            "apply_scope": "请求级切换，不修改 conf.d/llm.json",
        }

    def execute_tool(self, name: str, args: Dict[str, Any], created_approvals: List[Dict[str, Any]]) -> Dict[str, Any]:
        """执行单个受控工具；高危删除/中止强制走审批单，绝不直接执行。"""
        if name == "list_nodes":
            return {"nodes": self.hub.list_nodes()}

        if name == "list_bots":
            return {"bots": self.hub.list_bots(include_archived=bool(args.get("include_archived", False)))}

        if name == "get_bot_progress":
            bot_id = str(args.get("bot_id") or "")
            bot = self.hub.repo.get_bot(bot_id)
            if not bot:
                return {"error": f"Bot '{bot_id}' 不存在"}
            limit = max(1, min(int(args.get("limit") or 20), 100))
            recent = self.hub.repo.list_recent_events(bot_id, limit=limit)
            return {"bot": bot, "recent_events": recent}

        if name == "get_dashboard_stats":
            days = int(args.get("days") or 7)
            return collect_dashboard_stats(self.hub, days=days)

        if name == "create_bot":
            node_id = str(args.get("node_id") or "").strip()
            if not self.hub.is_node_online(node_id):
                return {"ok": False, "error": f"目标 PC 节点 '{node_id}' 当前不在线，无法创建 Bot"}
            ok, res = self.hub.create_bot(
                name=str(args.get("name") or ""),
                node_id=node_id,
                engine=str(args.get("engine") or ""),
                cwd=str(args.get("cwd") or ""),
                approval_mode=str(args.get("approval_mode") or "auto"),
                model=args.get("model"),
            )
            return {"ok": ok, **res}

        if name == "dispatch_turn":
            ok, res = self.hub.start_turn(
                bot_id=str(args.get("bot_id") or ""),
                prompt=str(args.get("prompt") or ""),
                input_source="llm_manager",
            )
            return {"ok": ok, **res}

        if name == "request_delete_bot":
            bot_id = str(args.get("bot_id") or "")
            reason = str(args.get("reason") or "AI 总管建议清理").strip()
            bot = self.hub.repo.get_bot(bot_id)
            if not bot:
                return {"ok": False, "error": f"Bot '{bot_id}' 不存在"}
            appr = self.hub.repo.create_approval(
                bot_id=bot_id,
                turn_id=None,
                tool_name="delete_bot",
                summary=f"删除 Bot [{bot['name']}] ({bot['node_id']}/{bot['engine']}): {reason}",
                raw_request={"bot_id": bot_id, "reason": reason},
                approval_type=ApprovalType.BOT_DELETE.value,
                requested_by="llm_manager",
            )
            created_approvals.append(appr)
            self.hub._broadcast_clients({"type": "approval.created", "approval": appr})
            self.hub._notify(f"AI 总管发起删除授权 ({bot['name']})", appr["summary"], kind="approval", bot_id=bot_id)
            return {"ok": True, "status": "pending_approval", "approval": appr}

        if name == "request_cancel_turn":
            bot_id = str(args.get("bot_id") or "")
            reason = str(args.get("reason") or "AI 总管建议中止").strip()
            bot = self.hub.repo.get_bot(bot_id)
            if not bot:
                return {"ok": False, "error": f"Bot '{bot_id}' 不存在"}
            active = self.hub.repo.get_active_turn_for_bot(bot_id)
            if not active:
                return {"ok": False, "error": "该 Bot 当前没有运行中的回合"}
            appr = self.hub.repo.create_approval(
                bot_id=bot_id,
                turn_id=active["turn_id"],
                tool_name="cancel_turn",
                summary=f"中止 Bot [{bot['name']}] 当前回合: {reason}",
                raw_request={"bot_id": bot_id, "turn_id": active["turn_id"], "reason": reason},
                approval_type=ApprovalType.TURN_CANCEL.value,
                requested_by="llm_manager",
            )
            created_approvals.append(appr)
            self.hub._broadcast_clients({"type": "approval.created", "approval": appr})
            self.hub._notify(f"AI 总管发起中止授权 ({bot['name']})", appr["summary"], kind="approval", bot_id=bot_id)
            return {"ok": True, "status": "pending_approval", "approval": appr}

        return {"ok": False, "error": f"未授权的工具: {name}"}

    def chat(
        self,
        message: str,
        *,
        model_name: str = "",
        history: Optional[List[Dict[str, str]]] = None,
        max_tool_rounds: int = 4,
    ) -> Tuple[bool, Dict[str, Any]]:
        message = (message or "").strip()
        if not message:
            return False, {"error": "消息不能为空"}
        if self.gateway is None:
            return False, {"error": "宿主未配置 LLMGateway，无法启用 AI 总管"}

        catalog = self.list_models()
        available_names = {m["name"] for m in catalog["models"]}
        selected_model = (model_name or "").strip() or catalog["default_model"]
        if available_names and selected_model not in available_names:
            return False, {"error": f"模型 '{selected_model}' 不在 conf.d/llm.json 已配置目录中"}

        messages: List[Dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
        for h in (history or [])[-12:]:
            role = h.get("role")
            content = str(h.get("content") or "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})

        tool_trace: List[Dict[str, Any]] = []
        created_approvals: List[Dict[str, Any]] = []
        total_tokens = 0
        final_reply = ""

        for _ in range(max(1, min(max_tool_rounds, 6))):
            try:
                res = self.gateway.invoke_sync(
                    messages,
                    tools=MANAGER_TOOLS_SCHEMA,
                    model=selected_model,
                    role="pc_cli_manager",
                )
            except Exception as e:
                return False, {"error": f"调用模型 '{selected_model}' 失败: {e}"}

            total_tokens += int(res.get("usage_total") or 0)
            content = str(res.get("content") or "")
            tool_calls = res.get("tool_calls") or []

            if not tool_calls:
                final_reply = content
                break

            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                tname = str(fn.get("name") or tc.get("name") or "")
                raw_args = fn.get("arguments") if "arguments" in fn else tc.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args) if raw_args.strip() else {}
                    except Exception:
                        parsed_args = {}
                elif isinstance(raw_args, dict):
                    parsed_args = raw_args
                else:
                    parsed_args = {}

                out = self.execute_tool(tname, parsed_args, created_approvals)
                tool_trace.append({"tool": tname, "args": parsed_args, "result": out})
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(tc.get("id") or tname),
                    "name": tname,
                    "content": json.dumps(out, ensure_ascii=False)[:8000],
                })
        else:
            final_reply = "已执行完受控工具轮次，请查看上方工具执行记录与待审批卡片。"

        self.hub.repo.record_manager_usage(
            model_name=selected_model,
            tokens_used=total_tokens,
            tool_calls=len(tool_trace),
        )
        return True, {
            "reply": final_reply,
            "model": selected_model,
            "tool_trace": tool_trace,
            "created_approvals": created_approvals,
            "usage_total": total_tokens,
        }

"""
PC CLI 手机管理中枢契约与常量定义 (features.pc_cli.contracts).
纯标准库定义，供中枢插件内各模块共享。
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Dict, Optional, Tuple


class NodeStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class BotStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    ERROR = "error"


class TurnStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ApprovalType(str, Enum):
    CLI_TOOL = "cli_tool"
    BOT_DELETE = "bot_delete"
    TURN_CANCEL = "turn_cancel"


class ApprovalDecision(str, Enum):
    PENDING = "pending"
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"
    EXPIRED = "expired"


class EventType(str, Enum):
    USER_MESSAGE = "user_message"
    THINKING = "thinking"
    TOOL_START = "tool_start"
    TOOL_UPDATE = "tool_update"
    TOOL_END = "tool_end"
    TEXT_DELTA = "text_delta"
    ASSISTANT_MESSAGE = "assistant_message"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESOLVED = "approval_resolved"
    TURN_STATUS = "turn_status"
    ERROR = "error"


SUPPORTED_ENGINES: Tuple[str, ...] = ("codex", "agy")
APPROVAL_MODES: Tuple[str, ...] = ("auto", "ask")
EXECUTION_MODES: Tuple[str, ...] = ("agent", "plan", "chat")
REASONING_EFFORTS: Tuple[str, ...] = ("", "low", "medium", "high", "xhigh")
VALID_DECISIONS: Tuple[str, ...] = (
    ApprovalDecision.ALLOW_ONCE.value,
    ApprovalDecision.ALLOW_SESSION.value,
    ApprovalDecision.DENY.value,
)
MAX_EVENT_TEXT_CHARS = 65536
MAX_SYSTEM_PROMPT_CHARS = 8000
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")

# 可复用的 Bot 角色与约束模板（供新建 Bot 与 AI 总管 create_bot 工具统一复用）
BOT_INSTRUCTION_TEMPLATES: Dict[str, Dict[str, str]] = {
    "general_dev": {
        "label": "通用开发规范",
        "content": (
            "## 角色定位\n你是项目开发工程师，负责代码排查、修改与验证。\n\n"
            "## 约束规则\n"
            "- 严禁硬编码密码、Token、API Key 或私密配置。\n"
            "- 修改前先阅读相关上下文与现有规范（自动遵循工作目录中的 AGENTS.md）。\n"
            "- 改完代码后运行对应测试或语法校验，并简洁汇报改动与结果。"
        ),
    },
    "readonly_audit": {
        "label": "只读巡检专家",
        "content": (
            "## 角色定位\n你是只读巡检与分析专家，仅负责核查状态、日志与数据。\n\n"
            "## 约束规则\n"
            "- 严格只读：禁止修改代码/配置、禁止写库、禁止重启或停止线上服务。\n"
            "- 结论先总后分，关键指标与异常项优先用 Markdown 表格和证据行呈现。"
        ),
    },
    "bill_analyst": {
        "label": "账单查询助手",
        "content": (
            "## 角色定位\n你是个人财务与账单分析助手，负责查询账单、交易明细与解析健康度。\n\n"
            "## 约束规则\n"
            "- 仅以只读方式读取账单数据库与视图，严禁修改数据或回显邮箱密码/凭证配置。\n"
            "- 汇总金额、币种、异常告警时优先使用表格，并标明时间范围与口径。"
        ),
    },
    "ops_debugger": {
        "label": "安全运维排障",
        "content": (
            "## 角色定位\n你是服务器与系统运维排障助手，负责定位故障根因并给出最小化修复方案。\n\n"
            "## 约束规则\n"
            "- 先查日志、进程、端口与配置确认根因，不做猜测性修改。\n"
            "- 涉及服务重启或线上变更前，先说明影响面与回滚方式。"
        ),
    },
    "rss_meili_digest": {
        "label": "RSS与聚合情报专家",
        "content": (
            "## 角色定位\n你是 RSS 热点阅读、Meilisearch 信息聚合与邮件/日程简报专家。\n\n"
            "## 上下文与数据源\n"
            "- 围绕 `scripts/rss_topic`（热点聚类、主题演进、去重与推送）及 Meilisearch 中的新闻、邮件、待办与日程索引开展检索与提炼。\n\n"
            "## 约束规则\n"
            "- 默认以只读方式检索和汇总，不随意修改索引数据或触发线上推送脚本；如需回写或重跑先征求确认。\n"
            "- 输出按「核心要点 / 热点主题 / 邮件与日程提醒」结构化呈现，优先使用 Markdown 表格与时间线，严禁输出密钥或邮箱密码。"
        ),
    },
}


def resolve_bot_system_prompt(
    system_prompt: Optional[str] = None,
    instruction_template: Optional[str] = None,
) -> Tuple[bool, str, str]:
    """校验并合并可选模板与 Bot 专属角色/上下文/约束指令。返回 (ok, merged_text, error)。"""
    tpl_key = str(instruction_template or "").strip()
    custom = str(system_prompt or "").strip()
    parts: list[str] = []
    if tpl_key:
        tpl = BOT_INSTRUCTION_TEMPLATES.get(tpl_key)
        if not tpl:
            allowed = ", ".join(sorted(BOT_INSTRUCTION_TEMPLATES.keys()))
            return False, "", f"不支持的指令模板 '{tpl_key}'，可选: {allowed}"
        tpl_body = tpl["content"].strip()
        if tpl_body and tpl_body not in custom:
            parts.append(tpl_body)
    if custom:
        parts.append(custom)
    merged = "\n\n".join(parts).strip()
    if len(merged) > MAX_SYSTEM_PROMPT_CHARS:
        return False, "", f"角色与约束指令长度不能超过 {MAX_SYSTEM_PROMPT_CHARS} 字符（当前 {len(merged)}）"
    return True, merged, ""


def is_valid_model_name(model: Optional[str]) -> bool:
    """校验可选模型名，严禁空白或 shell/cmd 元字符。"""
    if model is None or model == "":
        return True
    return bool(MODEL_NAME_RE.match(model))


def now_ms() -> int:
    """返回当前 UTC 毫秒时间戳。"""
    return int(time.time() * 1000)

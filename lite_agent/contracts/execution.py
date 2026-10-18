"""
执行上下文与结果协议（纯契约定义）。
仅依赖 Python 标准库，深度不可变，无任何外部副作用。
"""

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Optional, List, Dict, Any, Tuple, Set


# ============================================================
#  Enums — str, Enum 保证 JSON/账本值稳定，不受 auto() 顺序影响
# ============================================================

class ActorType(str, Enum):
    """执行主体类型"""
    ADMIN = "admin"
    USER = "user"
    GUEST = "guest"
    WORKER = "worker"
    SYSTEM = "system"  # 系统自动触发（Cron、告警、定时任务）
    CRON = "cron"      # 定时任务


class ExecutionSource(str, Enum):
    """执行来源"""
    DIRECT = "direct"                # 直接调用（/ 命令、消息触发）
    ORCHESTRATOR = "orchestrator"   # 由编排器分派子任务
    STREAM = "stream"               # 流式 AI Loop
    CRON = "cron"                   # 定时任务触发
    API = "api"                     # 外部 API 调用
    LEGACY = "legacy"               # 旧接口兼容路径（标记为待迁移）


# ============================================================
#  深度冻结工具
# ============================================================

def _deep_freeze(value: Any) -> Any:
    """递归冻结：dict → MappingProxyType，list → tuple，其余原样返回。"""
    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_deep_freeze(v) for v in value)
    return value


# ============================================================
#  ExecutionContext
# ============================================================

@dataclass(frozen=True)
class ExecutionContext:
    """
    一次工具调用的执行上下文。

    集中管理原本分散在 is_guest、allowed_tools、max_steps 等参数中的
    执行主体、权限边界、预算和来源信息。
    """
    actor_id: str
    actor_type: ActorType = ActorType.USER
    source: ExecutionSource = ExecutionSource.DIRECT
    allowed_tools: Optional[frozenset] = None
    session_key: str = ""
    is_dry_run: bool = False
    max_steps: int = 8
    max_output_tokens: int = 2048
    token_budget: Optional[int] = None
    execution_id: str = ""
    parent_execution_id: str = ""

    def __post_init__(self):
        # 冻结 allowed_tools：任意可迭代 → frozenset
        if self.allowed_tools is not None and not isinstance(self.allowed_tools, frozenset):
            object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))

    @property
    def is_guest(self) -> bool:
        return self.actor_type == ActorType.GUEST

    @property
    def is_unrestricted(self) -> bool:
        """allowed_tools 为 None 表示不限制。"""
        return self.allowed_tools is None

    @property
    def tool_set(self) -> Optional[frozenset]:
        """返回 frozen set 形式的工具集合，别名属性。"""
        return self.allowed_tools

    def has_tool_access(self, tool_name: str) -> bool:
        """O(1) 检查是否允许调用指定工具。"""
        if self.allowed_tools is None:
            return True
        return tool_name in self.allowed_tools


# ============================================================
#  ExecutionResult
# ============================================================

@dataclass(frozen=True)
class ExecutionResult:
    """
    标准化工具执行结果。
    """
    ok: bool
    output: str
    tool_name: str = ""
    tool_args: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    tool_call_id: str = ""
    error_code: str = ""
    retryable: bool = False
    data: Optional[MappingProxyType] = None
    side_effects_performed: Tuple[str, ...] = ()

    def __post_init__(self):
        # 深度冻结 tool_args
        if not isinstance(self.tool_args, MappingProxyType):
            object.__setattr__(self, "tool_args",
                               _deep_freeze(dict(self.tool_args)))
        else:
            frozen = _deep_freeze(dict(self.tool_args))
            if frozen is not self.tool_args:
                object.__setattr__(self, "tool_args", frozen)
        # 深度冻结 data
        if self.data is not None and not isinstance(self.data, MappingProxyType):
            object.__setattr__(self, "data", _deep_freeze(dict(self.data)))
        elif self.data is not None:
            frozen = _deep_freeze(dict(self.data))
            if frozen is not self.data:
                object.__setattr__(self, "data", frozen)
        # 冻结 side_effects_performed
        if not isinstance(self.side_effects_performed, tuple):
            object.__setattr__(self, "side_effects_performed",
                               tuple(self.side_effects_performed))

    # ---- 工厂方法 ----

    @staticmethod
    def success(tool_name: str, tool_args: dict, output: str = "",
                data: dict = None, side_effects: list = None,
                tool_call_id: str = "") -> "ExecutionResult":
        return ExecutionResult(
            ok=True, output=output, tool_name=tool_name,
            tool_args=_deep_freeze(dict(tool_args)),
            tool_call_id=tool_call_id,
            data=_deep_freeze(dict(data)) if data else None,
            side_effects_performed=tuple(side_effects or ()),
        )

    @staticmethod
    def error(tool_name: str, tool_args: dict, error_code: str,
              error_msg: str, retryable: bool = False,
              tool_call_id: str = "") -> "ExecutionResult":
        return ExecutionResult(
            ok=False, output=error_msg, tool_name=tool_name,
            tool_args=_deep_freeze(dict(tool_args)),
            tool_call_id=tool_call_id,
            error_code=error_code, retryable=retryable,
        )

    @staticmethod
    def unknown_skill(skill_name: str, arguments: str,
                      tool_call_id: str = "") -> "ExecutionResult":
        return ExecutionResult(
            ok=False,
            output=f"未知技能: {skill_name}",
            tool_name=skill_name,
            tool_args=_deep_freeze({"raw": arguments}),
            tool_call_id=tool_call_id,
            error_code="UNKNOWN_SKILL",
            retryable=False,
        )

    @staticmethod
    def invalid_args(skill_name: str, arguments: str, error: str,
                      tool_call_id: str = "") -> "ExecutionResult":
        return ExecutionResult(
            ok=False,
            output=f"参数解析失败: {error}",
            tool_name=skill_name,
            tool_args=_deep_freeze({"raw": arguments}),
            tool_call_id=tool_call_id,
            error_code="INVALID_ARGS",
            retryable=False,
        )

    @staticmethod
    def skill_error(skill_name: str, tool_args: dict, error: str,
                    retryable: bool = False,
                    side_effects: list = None,
                    tool_call_id: str = "") -> "ExecutionResult":
        return ExecutionResult(
            ok=False,
            output=f"技能执行异常 [{skill_name}]: {error}",
            tool_name=skill_name,
            tool_args=_deep_freeze(dict(tool_args)),
            tool_call_id=tool_call_id,
            error_code="SKILL_ERROR",
            retryable=retryable,
            side_effects_performed=tuple(side_effects or ()),
        )

    # ---- 序列化 ----

    def to_legacy_string(self) -> str:
        """转为旧 execute() 返回的字符串格式，供适配器使用。
        成功时直接返回 output，失败时添加 ❌ 前缀以保持向后兼容。"""
        if self.ok:
            return self.output
        return f"❌ {self.output}"

    def to_model_message(self, tool_call_id: str = "") -> dict:
        """
        生成标准的 OpenAI tool 消息 dict。
        统一 Agent/Worker 各自拼工具消息的逻辑，避免分散构造。
        """
        call_id = tool_call_id or self.tool_call_id
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": self.tool_name,
            "content": self.output,
        }


# ============================================================
#  SkillPolicy (skill metadata)
# ============================================================

@dataclass(frozen=True)
class SkillPolicy:
    """
    skill 装饰器的策略元数据。
    """
    side_effect: Optional[bool] = None
    supports_dry_run: bool = False
    guest_ok: bool = False
    guard_keywords: Tuple[str, ...] = ()
    guard_prompt: str = ""
    guard_threshold: int = 1

    def __post_init__(self):
        # 冻结 guard_keywords
        if not isinstance(self.guard_keywords, tuple):
            object.__setattr__(self, "guard_keywords",
                               tuple(self.guard_keywords))

    @property
    def can_dry_run(self) -> bool:
        return self.side_effect is not None and self.supports_dry_run

    @property
    def effective_side_effect(self) -> bool:
        return self.side_effect is not False

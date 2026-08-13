"""
Phase 1a — 执行上下文与结果协议

引入三个核心对象，统一 skill_engine、worker_agent、agent 之间的执行模型：

  ExecutionContext  — 明确执行主体、来源、工具权限与预算，替代散落的参数
  ExecutionResult   — 标准化工具返回值，用 ok/output/error_code 替代纯字符串
  SkillPolicy       — 为 skill 装饰器增加 side_effect / supports_dry_run 元数据

设计原则：
  - 纯数据对象，不依赖任何外部模块
  - 深度不可变：frozen=True + __post_init__ 中递归冻结所有容器字段
  - 枚举值稳定：str, Enum 保证 JSON/账本可持久化
  - 向后兼容：旧 execute() 返回 str 的接口在 Phase 1b 中通过适配器保持
"""

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Optional


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

def _deep_freeze(value):
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

    Attributes:
        actor_id: 调用方标识（user_id / worker_name / cron_job_id）
        actor_type: 调用方类型
        source: 执行来源
        allowed_tools: 允许的工具名称；None 表示不限制
        session_key: 关联的会话标识，用于审计和限流
        is_dry_run: 是否 dry-run 模式
        max_steps: 最大工具调用步数
        max_output_tokens: 单次模型调用最大输出 token
        token_budget: 可选累计 token 预算上限；None 表示不限制
        execution_id: 本次 Runtime 执行的唯一标识（ULID/UUID），由 Ledger 分配
        parent_execution_id: 父执行 ID；Worker 子任务指向 Orchestrator 任务，普通聊天为空
    """
    actor_id: str
    actor_type: ActorType = ActorType.USER
    source: ExecutionSource = ExecutionSource.DIRECT
    allowed_tools: Optional[frozenset[str]] = None
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
    def tool_set(self) -> Optional[frozenset[str]]:
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

    替代当前 execute() 返回的纯字符串，消除调用方对 "❌" 前缀的脆弱解析。

    Attributes:
        ok: 执行是否成功
        output: 工具返回的文本内容（成功时 = 结果，失败时 = error_msg）
        tool_name: 被调用的工具名称
        tool_args: 解析后的参数字典（深度不可变）
        tool_call_id: OpenAI tool_call id，用于消息关联
        error_code: 标准化错误码
        retryable: 该错误是否可重试（如超时 → True，权限错误 → False）
        data: 结构化数据（深度不可变）
        side_effects_performed: 实际已执行的副作用列表（不可变）
    """
    ok: bool
    output: str
    tool_name: str = ""
    tool_args: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    tool_call_id: str = ""
    error_code: str = ""
    retryable: bool = False
    data: Optional[MappingProxyType] = None
    side_effects_performed: tuple[str, ...] = ()

    def __post_init__(self):
        # 深度冻结 tool_args
        if not isinstance(self.tool_args, MappingProxyType):
            object.__setattr__(self, "tool_args",
                               _deep_freeze(dict(self.tool_args)))
        else:
            # 即使已经是 MappingProxyType，内部嵌套仍可能可变 → 重新深度冻结
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
                data: dict = None, side_effects: list[str] = None,
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
                    side_effects: list[str] = None,
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
        如果未显式传入 tool_call_id，使用 self.tool_call_id。
        """
        call_id = tool_call_id or self.tool_call_id
        msg = {
            "role": "tool",
            "tool_call_id": call_id,
            "name": self.tool_name,
            "content": self.output,
        }
        return msg


# ============================================================
#  SkillPolicy (skill metadata)
# ============================================================

@dataclass(frozen=True)
class SkillPolicy:
    """
    skill 装饰器的策略元数据。

    将 guest_ok / guard_keywords 等分散参数统一为结构化元数据。

    Attributes:
        side_effect: 该技能是否有副作用。
                     True  = 有副作用（写 DB、发消息、重启服务等）
                     False = 无副作用（纯查询）
                     None  = 副作用未知（历史技能未声明），保守处理为有副作用
        supports_dry_run: 该技能是否显式声明支持 dry-run 模式。
        guest_ok: 是否允许访客调用。
        guard_keywords: 触发 guard prompt 的关键词（不可变）。
        guard_prompt: 数据忠实执行指令。
        guard_threshold: 触发 guard 的最小关键词匹配数。
    """
    side_effect: Optional[bool] = None
    supports_dry_run: bool = False
    guest_ok: bool = False
    guard_keywords: tuple[str, ...] = ()
    guard_prompt: str = ""
    guard_threshold: int = 1

    def __post_init__(self):
        # 冻结 guard_keywords
        if not isinstance(self.guard_keywords, tuple):
            object.__setattr__(self, "guard_keywords",
                               tuple(self.guard_keywords))

    @property
    def can_dry_run(self) -> bool:
        """已知副作用类型（True 或 False）且 supports_dry_run 时方可 dry-run。
        写操作（side_effect=True）是最需要预演的，不应排除。"""
        return self.side_effect is not None and self.supports_dry_run

    @property
    def effective_side_effect(self) -> bool:
        """保守处理：None（未知）视为有副作用。"""
        return self.side_effect is not False
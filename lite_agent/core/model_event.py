"""ModelEvent - LLM 流式事件，协议无关，深度不可变。

类型通过 ModelEventType 枚举约束，不允许任意字符串。
data 和 meta 中的 dict / list 递归冻结为 MappingProxyType / tuple，
构造后无法修改任何层级的数据。
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from types import MappingProxyType
from typing import Any


class ModelEventType(Enum):
    """ModelEvent 类型枚举，不允许任意字符串。"""
    TEXT = auto()
    REASONING = auto()
    TOOL_CALL_DELTA = auto()
    USAGE = auto()
    DONE = auto()
    ERROR = auto()


def _deep_freeze(value: Any) -> Any:
    """递归冻结：dict -> MappingProxyType，list -> tuple，其他原样返回。"""
    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class ModelEvent:
    """LLM 流式事件，协议无关。深度不可变。

    - data 和 meta 中的 dict 递归冻结为 MappingProxyType
    - data 和 meta 中的 list 递归冻结为 tuple
    - type 必须是 ModelEventType 枚举成员，不接受字符串
    """

    type: ModelEventType
    """事件类型枚举，不接受字符串"""

    data: Any = None
    """事件负载（深度冻结后不可变）:
       - TEXT: str (delta 文本)
       - REASONING: str (推理链 delta)
       - TOOL_CALL_DELTA: MappingProxyType (单次 tool_call 分片: {index, id?, name?, arguments?})
       - USAGE: MappingProxyType (token 用量快照: {prompt_tokens, completion_tokens, total_tokens})
       - DONE: MappingProxyType (完成信息: {finish_reason})
       - ERROR: Exception (异常对象)
    """

    meta: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    """附加元数据（深度冻结后不可变，仅用于透传）"""

    def __post_init__(self):
        """校验类型并冻结 data 和 meta 中的可变容器。"""
        if not isinstance(self.type, ModelEventType):
            raise TypeError(
                f"ModelEvent.type 必须是 ModelEventType 枚举，"
                f"收到 {self.type!r} (类型 {type(self.type).__name__})"
            )
        object.__setattr__(self, 'data', _deep_freeze(self.data))
        if not isinstance(self.meta, MappingProxyType):
            object.__setattr__(self, 'meta', _deep_freeze(self.meta) if isinstance(self.meta, dict) else MappingProxyType({}))

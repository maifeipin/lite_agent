"""
Lite Agent 会话管理兼容薄入口。
规范实现位于 lite_agent.session。
"""

from lite_agent.session import (
    Session,
    SessionManager,
    _pack_json_value,
    _unpack_json_value,
)

__all__ = ["Session", "SessionManager"]

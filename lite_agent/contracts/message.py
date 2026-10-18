"""
标准化通道层传输消息与回复协议。
仅依赖 Python 标准库，无外部环境副作用。
"""

from typing import List, Optional, Dict, Any


class IncomingMessage:
    """从通道层传入的标准化消息"""

    def __init__(self, channel: str, user_id: str, chat_id: str,
                 message_id: str, text: str, notify_channels: Optional[List[str]] = None,
                 is_guest: bool = False, sync_mode: bool = False,
                 channel_payload: Optional[Dict[str, Any]] = None, output_mode: str = ""):
        self.channel = channel
        self.user_id = user_id
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.is_guest = is_guest
        self.sync_mode = sync_mode
        # 如果未指定通知通道，默认只回复给来源通道 (避免跨通道广播)
        self.notify_channels = notify_channels if notify_channels is not None else [channel]
        # channel_payload: 通道层原始上下文, 供异步推送 (_push_result/send_progress) 使用
        # 如 钉钉的 msg_data(含sessionWebhook)、飞书的 sender open_id 等。默认 None 向后兼容。
        self.channel_payload = channel_payload or {}
        self.output_mode = output_mode
        self.model_override = ""
        self._session_key = ""

    @property
    def scope_key(self) -> str:
        """Stable channel/user identity used to find the active conversation."""
        return f"{self.channel}:{self.user_id}"

    @property
    def session_key(self) -> str:
        return self._session_key or self.scope_key

    def bind_session(self, session_key: str) -> None:
        self._session_key = session_key


class AgentResponse:
    """Agent 返回给通道层的标准化回复"""

    def __init__(self, text: str, title: str = "", color: str = "blue",
                 task_id: str = "", logs: Optional[List[Any]] = None,
                 new_session_key: str = ""):
        self.text = text
        self.title = title
        self.color = color
        self.task_id = task_id
        self.logs = logs if logs is not None else []
        self.new_session_key = new_session_key

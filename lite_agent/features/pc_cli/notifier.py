"""
PC CLI 异步 IM 提醒模块 (features.pc_cli.notifier).
仅作为辅助提醒通道：当长任务完成、失败或挂起等待审批时，构造标准 AgentResponse
并通过后台有界工作线程异步投递给宿主 send_owner_response，绝不阻塞 WebSocket 事件循环。
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Optional

from lite_agent.contracts.message import AgentResponse

logger = logging.getLogger("features.pc_cli.notifier")


class PcCliNotifier:
    """将 Hub 关键状态事件封装为 AgentResponse 异步投递给宿主渠道。"""

    def __init__(
        self,
        send_fn: Optional[Callable[[AgentResponse], Any]] = None,
        *,
        notify_on_complete: bool = True,
        notify_on_approval: bool = True,
        mobile_base_url: str = "",
        max_queue_size: int = 128,
    ):
        self._send_fn = send_fn
        self.notify_on_complete = notify_on_complete
        self.notify_on_approval = notify_on_approval
        self.mobile_base_url = (mobile_base_url or "").strip().rstrip("/")
        self._queue: queue.Queue[AgentResponse] = queue.Queue(maxsize=max_queue_size)
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        if self._send_fn is not None:
            self._worker = threading.Thread(target=self._worker_loop, name="pc-cli-notifier", daemon=True)
            self._worker.start()

    def close(self, timeout: float = 2.0) -> None:
        """安全停止后台线程：基于 stop_event，即使队列已满也不会丢失退出信号。"""
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=timeout)

    def build_response(self, title: str, content: str, *, kind: str = "complete", bot_id: Optional[str] = None) -> AgentResponse:
        lines = []
        body = (content or "").strip()
        if body:
            lines.append(body[:300])
        if self.mobile_base_url:
            link = f"{self.mobile_base_url}/#bot/{bot_id}" if bot_id else f"{self.mobile_base_url}/"
            lines.append(f"查看详情: {link}")
        color = "orange" if kind == "approval" else "blue"
        return AgentResponse(
            text="\n".join(lines) or title,
            title=f"PC CLI · {title}",
            color=color,
        )

    def _should_notify(self, kind: str) -> bool:
        if not self._send_fn or self._stop_event.is_set():
            return False
        if kind == "approval" and not self.notify_on_approval:
            return False
        if kind == "complete" and not self.notify_on_complete:
            return False
        return True

    def notify(self, title: str, content: str, *, kind: str = "complete", bot_id: Optional[str] = None) -> bool:
        """同步发送并返回宿主真实投递结果（bool）。"""
        if not self._should_notify(kind):
            return False
        resp = self.build_response(title, content, kind=kind, bot_id=bot_id)
        try:
            return bool(self._send_fn(resp))
        except Exception as e:
            logger.debug(f"发送 PC CLI 提醒失败: {e}")
            return False

    def notify_async(self, title: str, content: str, *, kind: str = "complete", bot_id: Optional[str] = None) -> bool:
        """非阻塞入队由后台线程投递，不阻塞 WebSocket 或 HTTP 主路径。"""
        if not self._should_notify(kind):
            return False
        resp = self.build_response(title, content, kind=kind, bot_id=bot_id)
        try:
            self._queue.put_nowait(resp)
            return True
        except queue.Full:
            logger.warning("PC CLI 提醒队列已满，丢弃一条非关键提醒")
            return False

    def _worker_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.15)
            except queue.Empty:
                if self._stop_event.is_set():
                    break
                continue
            try:
                if self._send_fn:
                    ok = bool(self._send_fn(item))
                    if not ok:
                        logger.debug(f"宿主渠道未投递 PC CLI 提醒: {item.title}")
            except Exception as e:
                logger.debug(f"后台发送 PC CLI 提醒失败: {e}")

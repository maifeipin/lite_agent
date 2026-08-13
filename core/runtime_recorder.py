"""
Phase 3a — Runtime 事件 → 账本记录器

连接 AgentRuntime 和 ExecutionLedger，不破坏二者职责边界。

核心职责：
  - 将 RuntimeEvent 转换为脱敏的账本 payload
  - 通过 seq 保证事件顺序可还原
  - 更新 execution 表状态 (steps / token / status)
  - 所有写入 try/except，不阻断 Agent
  - TEXT/REASONING delta 按时间/大小聚合，批量单事务写入，降低写放大
  - 事件去重与概要投影原子化 (record_and_project)，重放不重复计费

敏感数据策略 (默认不保存):
  - 凭据/密码/API Key: 完全不存
  - TEXT: 仅长度 + SHA-256 + 前 200 字脱敏预览
  - REASONING: 仅长度 + SHA-256 (不保存正文)
  - TOOL_CALL arguments: 脱敏后最多 2 KB
  - TOOL_RESULT output: 完整结果已写入 Session，账本只存
    长度 + SHA-256 + 前 4 KB 脱敏预览

调用方式:
    execution = ledger.start(ctx, model_name)
    for event in recorder.wrap(execution.id, runtime.run(...)):
        consume(event)
    ledger.finish(execution.id)
"""

import hashlib
import logging
import time
from types import MappingProxyType
from typing import Iterator, Optional

from core.agent_runtime import RuntimeEvent, RuntimeEventType
from core.execution_ledger import ExecutionLedger
from core.utils.masker import mask_secrets

logger = logging.getLogger(__name__)


# ============================================================
#  截断阈值
# ============================================================

_TEXT_PREVIEW_LIMIT = 200        # TEXT 事件预览长度 (脱敏后)
_ARGS_LIMIT = 2048               # TOOL_CALL arguments 脱敏上限 (2 KB)
_OUTPUT_PREVIEW_LIMIT = 4096     # TOOL_RESULT output 预览上限 (4 KB)

# 批量聚合阈值: TEXT/REASONING delta 累积到任一阈值即 flush
_BATCH_TIME_THRESHOLD = 0.15     # 150 ms
_BATCH_SIZE_THRESHOLD = 4096     # 4 KB 累积字符


# ============================================================
#  RuntimeRecorder
# ============================================================

class RuntimeRecorder:
    """将 RuntimeEvent 记录到 ExecutionLedger。

    每个 execution 对应一个 recorder 实例；seq 从 0 单调递增。
    Agent/Worker 不必各写一遍事件记录逻辑，通过 wrap() 一行接入。

    写入策略:
      - TEXT/REASONING: 缓冲累积，按时间(150ms)或大小(4KB)聚合后批量写入
      - 其他事件: 立即通过 record_and_project 原子写入
      - wrap() 结束时 flush 剩余缓冲

    原子性:
      - record_and_project 在同一事务中 INSERT + 投影概要
      - INSERT OR IGNORE 保证 (execution_id, seq) 去重
      - 仅 INSERT 真正生效时才更新概要，重放不会重复计费
    """

    def __init__(self, ledger: ExecutionLedger, execution_id: str):
        self.ledger = ledger
        self.execution_id = execution_id
        self._seq = 0
        # 批量缓冲: 累积待写入的事件 dict 列表
        self._batch: list[dict] = []
        self._batch_chars = 0
        self._batch_start_time: Optional[float] = None

    # ============================================================
    #  公开 API
    # ============================================================

    def record(self, event: RuntimeEvent) -> None:
        """记录一条事件，并更新 execution 状态。

        TEXT/REASONING 事件进入批量缓冲；其他事件立即原子写入。
        若缓冲达到阈值，自动 flush。
        """
        event_type = event.type.name
        payload = self._build_payload(event)

        if event.type in (RuntimeEventType.TEXT, RuntimeEventType.REASONING):
            # 缓冲 TEXT/REASONING delta，聚合后批量写入
            self._buffer_event(event_type, payload)
            self._maybe_flush()
        else:
            # 非 delta 事件: 先 flush 缓冲，再立即原子写入
            self.flush()
            self._record_atomic(event_type, payload)

    def flush(self) -> None:
        """将缓冲的 TEXT/REASONING 事件批量写入 (单事务)。"""
        if not self._batch:
            return
        try:
            self.ledger.record_batch(self.execution_id, self._batch)
        except Exception:
            logger.exception(
                "RuntimeRecorder.flush 失败 (execution=%s, batch=%d)",
                self.execution_id, len(self._batch))
        finally:
            self._batch.clear()
            self._batch_chars = 0
            self._batch_start_time = None

    def wrap(self, runtime_iter: Iterator[RuntimeEvent]) -> Iterator[RuntimeEvent]:
        """生成器包装器：透明转发 Runtime 事件，同时记录到账本。

        用法：
            for event in recorder.wrap(runtime.run(...)):
                consume(event)
            # 生成器结束后，execution 终态由事件流中的终态事件决定；
            # 若 Runtime 异常退出未发终态事件，调用方应显式 ledger.finish()。

        失败策略：账本写入异常不阻断事件转发。
        wrap 结束时自动 flush 剩余缓冲。
        """
        try:
            for event in runtime_iter:
                try:
                    self.record(event)
                except Exception:
                    logger.exception(
                        "RuntimeRecorder.record 失败 (execution=%s, seq=%d)",
                        self.execution_id, self._seq)
                yield event
            # 正常结束: flush 剩余缓冲
            self.flush()
        except Exception:
            # Runtime 自身抛异常：flush 剩余 + 标记 execution 失败
            try:
                self.flush()
            except Exception:
                pass
            self.ledger.finish(self.execution_id,
                               status="failed", terminal_reason="runtime_exception")
            raise

    # ============================================================
    #  内部: 缓冲与写入
    # ============================================================

    def _buffer_event(self, event_type: str, payload: dict) -> None:
        """将 TEXT/REASONING 事件加入批量缓冲。"""
        seq = self._seq
        self._seq += 1
        self._batch.append({
            "seq": seq,
            "event_type": event_type,
            "payload": payload,
            "step": None,
            "tool_call_id": None,
            "tool_name": None,
        })
        # 累积字符数用于大小阈值判断
        self._batch_chars += payload.get("length", 0)
        if self._batch_start_time is None:
            self._batch_start_time = time.monotonic()

    def _maybe_flush(self) -> None:
        """检查是否达到批量阈值，达到则 flush。"""
        if not self._batch:
            return
        # 大小阈值
        if self._batch_chars >= _BATCH_SIZE_THRESHOLD:
            self.flush()
            return
        # 时间阈值
        if self._batch_start_time is not None:
            elapsed = time.monotonic() - self._batch_start_time
            if elapsed >= _BATCH_TIME_THRESHOLD:
                self.flush()

    def _record_atomic(self, event_type: str, payload: dict) -> None:
        """通过 record_and_project 原子写入事件并投影概要。"""
        seq = self._seq
        self._seq += 1
        tool_call_id = payload.get("tool_call_id") or payload.get("id")
        tool_name = payload.get("name") or payload.get("tool_name")
        step = payload.get("step")
        self.ledger.record_and_project(
            execution_id=self.execution_id,
            seq=seq,
            event_type=event_type,
            payload=payload,
            step=step if step is not None else None,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )

    # ============================================================
    #  payload 构造
    # ============================================================

    def _build_payload(self, event: RuntimeEvent) -> dict:
        """根据事件类型构造脱敏后的 payload。

        各事件类型对应的数据保护策略：
          TEXT/REASONING: 仅 length + sha256 + (TEXT 额外 200 字预览)
          TOOL_CALL: arguments 脱敏后 ≤ 2 KB
          TOOL_RESULT: output 仅 length + sha256 + ≤ 4 KB 预览
          其他: 原始 data (已是非敏感元数据)
        """
        t = event.type
        data = event.data or {}

        if t == RuntimeEventType.TEXT:
            text = data if isinstance(data, str) else str(data)
            return self._text_payload(text, with_preview=True)

        if t == RuntimeEventType.REASONING:
            text = data if isinstance(data, str) else str(data)
            return self._text_payload(text, with_preview=False)

        if t == RuntimeEventType.TOOL_CALL:
            return self._tool_call_payload(data)

        if t == RuntimeEventType.TOOL_RESULT:
            return self._tool_result_payload(data)

        if t == RuntimeEventType.TOOL_CALLS_READY:
            return self._tool_calls_ready_payload(data)

        # 其他事件: data 已是元数据 (step/usage/finish_reason/msg 等)
        # 复制一份，避免外部修改；RuntimeEvent.data 可能被 _deep_freeze 为 MappingProxyType
        if isinstance(data, (dict, MappingProxyType)):
            return dict(data)
        return {"data": data}

    # ---- 各事件 payload 构造 ----

    @staticmethod
    def _text_payload(text: str, with_preview: bool) -> dict:
        """TEXT/REASONING payload: length + sha256 + (可选)预览。"""
        payload = {
            "length": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        if with_preview:
            preview = mask_secrets(text[:_TEXT_PREVIEW_LIMIT])
            payload["preview"] = preview
        return payload

    @staticmethod
    def _tool_call_payload(data: dict) -> dict:
        """TOOL_CALL payload: 脱敏 arguments (≤ 2 KB) + step。"""
        args = data.get("arguments", "") or ""
        safe_args = mask_secrets(args)
        safe_args = safe_args[:_ARGS_LIMIT]
        return {
            "id": data.get("id", ""),
            "name": data.get("name", ""),
            "arguments_redacted": safe_args,
            "arguments_hash": hashlib.sha256(args.encode("utf-8")).hexdigest(),
            "arguments_length": len(args),
            "step": data.get("step"),
        }

    @staticmethod
    def _tool_result_payload(data: dict) -> dict:
        """TOOL_RESULT payload: 脱敏 output 预览 (≤ 4 KB) + step + duration_ms。"""
        output = data.get("output", "") or ""
        safe_preview = mask_secrets(output[:_OUTPUT_PREVIEW_LIMIT])
        return {
            "id": data.get("id", ""),
            "name": data.get("name", ""),
            "ok": data.get("ok", False),
            "output_length": len(output),
            "output_hash": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "output_preview": safe_preview,
            "duration_ms": data.get("duration_ms"),
            "step": data.get("step"),
            "side_effects": list(data.get("side_effects", [])),
        }

    @staticmethod
    def _tool_calls_ready_payload(data: dict) -> dict:
        """TOOL_CALLS_READY payload: 工具声明摘要 (仅 name + args_hash)。"""
        tool_calls = data.get("tool_calls", []) or []
        summary = []
        for tc in tool_calls:
            args = tc.get("function", {}).get("arguments", "") or ""
            summary.append({
                "id": tc.get("id", ""),
                "name": tc.get("function", {}).get("name", ""),
                "arguments_hash": hashlib.sha256(args.encode("utf-8")).hexdigest(),
                "arguments_length": len(args),
            })
        return {
            "content_length": len(data.get("content", "") or ""),
            "tool_calls_summary": summary,
        }

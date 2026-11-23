"""统一 LLM 调用错误分类契约。

统一将外部异常、HTTP 状态码及 ModelEventType.ERROR 事件映射为明确的枚举类别，
指导 Gateway、Orchestrator 与重试策略进行有界重试或安全熔断。
"""

from enum import Enum
from typing import Union


class LLMErrorCategory(str, Enum):
    AUTH_401 = "auth_401"                # 凭据失效/未授权 (401)
    PERMISSION_403 = "permission_403"    # 权限不足/未开通模型 (403)
    RATE_LIMIT_429 = "rate_limit_429"    # 频控限流 (429)
    QUOTA_EXHAUSTED = "quota_exhausted"  # 账户欠费/额度耗尽
    SERVER_5XX = "server_5xx"            # 服务端暂时性故障 / 连接异常 (500/502/503/504)
    BAD_REQUEST_400 = "bad_request_400"  # 参数/上下文超长/格式非法 (400)
    CONTENT_FILTER = "content_filter"    # 安全过滤/内容拒绝
    CLIENT_CANCELLED = "cancelled"       # 客户端取消/超时
    UNKNOWN = "unknown"                  # 未知错误


def classify_llm_error(error_or_msg: Union[Exception, str]) -> LLMErrorCategory:
    """对 LLM 异常或 ERROR 事件消息进行规范化分类。"""
    text = str(error_or_msg).lower()
    if any(k in text for k in ("401", "unauthorized", "invalid api key", "authentication error", "invalid_api_key")):
        return LLMErrorCategory.AUTH_401
    if any(k in text for k in ("403", "forbidden", "permission_denied", "access_denied")):
        return LLMErrorCategory.PERMISSION_403
    if any(k in text for k in ("insufficient_quota", "quota_exhausted", "balance_exhausted", "account_deactivated", "欠费", "余额不足")):
        return LLMErrorCategory.QUOTA_EXHAUSTED
    if any(k in text for k in ("429", "rate_limit", "rate limit", "resource has been exhausted", "too many requests", "resourceexhausted")):
        return LLMErrorCategory.RATE_LIMIT_429
    if any(k in text for k in ("500", "502", "503", "504", "bad gateway", "service unavailable", "internal server error", "connection reset", "connection refused", "timeout", "timed out")):
        return LLMErrorCategory.SERVER_5XX
    if any(k in text for k in ("400", "context_length_exceeded", "invalid_request_error", "bad request", "maximum context length")):
        return LLMErrorCategory.BAD_REQUEST_400
    if any(k in text for k in ("safety", "blocked", "content_filter", "content filter", "finish_reason: safety")):
        return LLMErrorCategory.CONTENT_FILTER
    return LLMErrorCategory.UNKNOWN


def is_retryable_error(category: LLMErrorCategory) -> bool:
    """判定指定错误类别是否允许在单次调用边界内进行重试。
    401/403/400/内容过滤等确定性错误坚决不盲目重试；仅 5xx/429 允许在受控预算内重试。
    """
    return category in (LLMErrorCategory.SERVER_5XX, LLMErrorCategory.RATE_LIMIT_429)

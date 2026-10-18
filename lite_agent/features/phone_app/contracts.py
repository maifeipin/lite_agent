"""
Phone App 数据契约与模型定义 (features.phone_app.contracts).
包含严格的状态迁移表、三态判定枚举、商品/店铺/物流筛选结构与任务回执契约。
严格遵循 phone-app-execution-development-20261018.md 规范。
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, Any, List, Optional, Set
import time


class BatchStatus(str, Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    NEEDS_HUMAN_ACTION = "needs_human_action"
    CANCELLED = "cancelled"
    EXECUTION_UNKNOWN = "execution_unknown"


class ConditionEvaluation(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class OverallDiagnosis(str, Enum):
    MATCHED = "matched"
    NEEDS_VERIFICATION = "needs_verification"
    REJECTED = "rejected"


class DeviceState(str, Enum):
    READY = "ready"
    BUSY = "busy"
    PAUSED = "paused"
    NEEDS_HUMAN_ACTION = "needs_human_action"
    OFFLINE = "offline"


# 合法状态迁移表
BATCH_TRANSITIONS: Dict[BatchStatus, Set[BatchStatus]] = {
    BatchStatus.DRAFT: {BatchStatus.PENDING_REVIEW, BatchStatus.APPROVED, BatchStatus.QUEUED, BatchStatus.CANCELLED},
    BatchStatus.PENDING_REVIEW: {BatchStatus.APPROVED, BatchStatus.DRAFT, BatchStatus.CANCELLED},
    BatchStatus.APPROVED: {BatchStatus.QUEUED, BatchStatus.CANCELLED, BatchStatus.PENDING_REVIEW},
    BatchStatus.QUEUED: {BatchStatus.RUNNING, BatchStatus.CANCELLED},
    BatchStatus.RUNNING: {
        BatchStatus.SUCCEEDED, BatchStatus.PARTIAL_FAILED, BatchStatus.FAILED,
        BatchStatus.CANCEL_REQUESTED, BatchStatus.CANCELLED
    },
    BatchStatus.CANCEL_REQUESTED: {BatchStatus.CANCELLED, BatchStatus.FAILED},
    BatchStatus.SUCCEEDED: set(),
    BatchStatus.PARTIAL_FAILED: set(),
    BatchStatus.FAILED: set(),
    BatchStatus.CANCELLED: set(),
}

TASK_TRANSITIONS: Dict[TaskStatus, Set[TaskStatus]] = {
    TaskStatus.DRAFT: {TaskStatus.PENDING_REVIEW, TaskStatus.APPROVED, TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.PENDING_REVIEW: {TaskStatus.APPROVED, TaskStatus.DRAFT, TaskStatus.CANCELLED},
    TaskStatus.APPROVED: {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.PENDING_REVIEW},
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.TIMED_OUT,
        TaskStatus.NEEDS_HUMAN_ACTION, TaskStatus.CANCEL_REQUESTED, TaskStatus.CANCELLED, TaskStatus.EXECUTION_UNKNOWN
    },
    TaskStatus.CANCEL_REQUESTED: {TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.TIMED_OUT: set(),
    TaskStatus.NEEDS_HUMAN_ACTION: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.CANCELLED: set(),
    TaskStatus.EXECUTION_UNKNOWN: set(),
}

# 回执状态规范化映射表，杜绝未知状态静默转成功
RECEIPT_STATUS_MAP: Dict[str, TaskStatus] = {
    "succeeded": TaskStatus.SUCCEEDED,
    "success": TaskStatus.SUCCEEDED,
    "failed": TaskStatus.FAILED,
    "timed_out": TaskStatus.TIMED_OUT,
    "timeout": TaskStatus.TIMED_OUT,
    "needs_human_action": TaskStatus.NEEDS_HUMAN_ACTION,
    "cancelled": TaskStatus.CANCELLED,
    "execution_unknown": TaskStatus.EXECUTION_UNKNOWN,
}


@dataclass
class SearchBudget:
    max_candidates: int = 20
    max_detail_visits: int = 10
    max_results: int = 5
    total_timeout_seconds: int = 180

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def validate(self) -> Tuple[bool, Optional[str]]:
        if self.max_candidates < 1 or self.max_candidates > 100:
            return False, "max_candidates 必须在 1 到 100 之间"
        if self.max_detail_visits < 0 or self.max_detail_visits > 50:
            return False, "max_detail_visits 必须在 0 到 50 之间"
        if self.max_results < 1 or self.max_results > 50:
            return False, "max_results 必须在 1 到 50 之间"
        if self.total_timeout_seconds <= 0 or self.total_timeout_seconds > 600:
            return False, "total_timeout_seconds 必须在 1 到 600 秒之间"
        return True, None


@dataclass
class SalesFilter:
    metric: str = "cumulative_sales"  # cumulative_sales | monthly_sales | payment_count
    operator: str = "gt"              # gt | gte | eq
    threshold: int = 1000

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PositiveRateFilter:
    operator: str = "gt"              # gt | gte
    threshold_percent: float = 95.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ShopTypesFilter:
    types: List[str] = field(default_factory=lambda: ["self_operated", "official_flagship"])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LogisticsFilter:
    free_shipping: Optional[bool] = None
    delivery_partner: Optional[str] = None  # e.g., "jd_delivery"
    destination_region: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProductFilters:
    sales: Optional[SalesFilter] = None
    positive_rate: Optional[PositiveRateFilter] = None
    shop_types: Optional[ShopTypesFilter] = None
    logistics: Optional[LogisticsFilter] = None

    def to_dict(self) -> Dict[str, Any]:
        res = {}
        if self.sales:
            res["sales"] = self.sales.to_dict()
        if self.positive_rate:
            res["positive_rate"] = self.positive_rate.to_dict()
        if self.shop_types:
            res["shop_types"] = self.shop_types.to_dict()
        if self.logistics:
            res["logistics"] = self.logistics.to_dict()
        return res


@dataclass
class ConditionResult:
    status: ConditionEvaluation  # pass | fail | unknown
    metric_name: str
    target_expression: str
    raw_text: Optional[str]
    parsed_value: Any
    scope: str                   # e.g., "item", "sku", "shop"
    evidence: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "metric_name": self.metric_name,
            "target_expression": self.target_expression,
            "raw_text": self.raw_text,
            "parsed_value": self.parsed_value,
            "scope": self.scope,
            "evidence": self.evidence,
            "reason": self.reason,
        }


@dataclass
class ProductItem:
    app_id: str
    source_task_id: str
    source_item_id: str
    title: str
    raw_price_text: Optional[str] = None
    amount: Optional[float] = None
    currency: str = "CNY"
    price_type: str = "未知"
    shop_name: Optional[str] = None
    shop_type: Optional[str] = None
    sales_info: Dict[str, Any] = field(default_factory=dict)
    positive_rate: Optional[float] = None
    logistics_info: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)
    evaluations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    diagnosis: OverallDiagnosis = OverallDiagnosis.NEEDS_VERIFICATION
    url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_id": self.app_id,
            "source_task_id": self.source_task_id,
            "source_item_id": self.source_item_id,
            "title": self.title,
            "raw_price_text": self.raw_price_text,
            "amount": self.amount,
            "currency": self.currency,
            "price_type": self.price_type,
            "shop_name": self.shop_name,
            "shop_type": self.shop_type,
            "sales_info": self.sales_info,
            "positive_rate": self.positive_rate,
            "logistics_info": self.logistics_info,
            "evidence": self.evidence,
            "evaluations": self.evaluations,
            "diagnosis": self.diagnosis.value,
            "url": self.url,
        }


@dataclass
class TaskReceipt:
    schema_version: str = "1.0.0"
    batch_id: str = ""
    task_id: str = ""
    attempt_id: str = ""
    app_id: str = ""
    app_name: str = ""
    package_name: str = ""
    app_version: str = ""
    adapter_version: str = ""
    device_id: str = ""
    driver_version: str = ""
    query: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    captured_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    partial: bool = True
    coverage: Dict[str, Any] = field(default_factory=dict)
    items: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    timings: Dict[str, int] = field(default_factory=lambda: {
        "queue": 0, "prepare": 0, "search": 0, "extract": 0, "reset": 0, "total": 0
    })
    reset_status: str = "ready"  # ready | failed | unverified
    artifact_refs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "app_id": self.app_id,
            "app_name": self.app_name,
            "package_name": self.package_name,
            "app_version": self.app_version,
            "adapter_version": self.adapter_version,
            "device_id": self.device_id,
            "driver_version": self.driver_version,
            "query": self.query,
            "status": self.status.value,
            "captured_at": self.captured_at,
            "partial": self.partial,
            "coverage": self.coverage,
            "items": self.items,
            "warnings": self.warnings,
            "error": self.error,
            "timings": self.timings,
            "reset_status": self.reset_status,
            "artifact_refs": self.artifact_refs,
        }

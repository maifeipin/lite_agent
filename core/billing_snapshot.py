"""Compatibility re-export layer for features.billing.snapshot.

已迁移至 features/billing/snapshot.py。
本文件作为过渡期向后兼容层保留，将于 E4 统一清理。
"""
import sys
from features.billing import snapshot as _impl
from features.billing.snapshot import (
    decimal_amount,
    month_start,
    build_snapshot,
    model_evidence,
    render_report,
    table,
)

__all__ = [
    'decimal_amount',
    'month_start',
    'build_snapshot',
    'model_evidence',
    'render_report',
    'table',
]

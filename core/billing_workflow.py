"""Compatibility re-export layer for features.billing.workflow.

已迁移至 features/billing/workflow.py。
本文件作为过渡期向后兼容层保留，将于 E4 统一清理。
"""
import sys
import subprocess
from features.billing import workflow as _impl
from features.billing.workflow import (
    _parse_json_summary,
    parse_json_summary,
    summarize_account_errors,
    _JSON_SUMMARY_RE,
)


class BillingWorkflow(_impl.BillingWorkflow):
    def fetch(self, script_dir, months, runner=None):
        return super().fetch(script_dir, months, runner=runner)


__all__ = [
    'BillingWorkflow',
    'subprocess',
    '_parse_json_summary',
    'parse_json_summary',
    'summarize_account_errors',
    '_JSON_SUMMARY_RE',
]

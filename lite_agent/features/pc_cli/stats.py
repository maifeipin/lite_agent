"""
PC CLI 总体统计看板聚合服务 (features.pc_cli.stats).
提供节点在线率、Bot 状态分布、近 N 天回合成功率/耗时与总管消耗统计。
"""

from __future__ import annotations

from typing import Any, Dict

from lite_agent.features.pc_cli.hub import PcCliHub


def collect_dashboard_stats(hub: PcCliHub, days: int = 7) -> Dict[str, Any]:
    """汇总总体看板统计快照。"""
    return hub.repo.get_dashboard_stats(days=days)

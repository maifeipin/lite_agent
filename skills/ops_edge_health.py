#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Edge 节点失联 sweep: 扫描 data/sentinel/edge_reports/*.json 的 mtime,
超过 staleness_min 未上报的节点 → 返回告警文本 (由 cron wrapper 推送)。

事件驱动的 _handle_edge_alert 抓不到"边缘彻底哑了" (没报告就没 handler 触发),
本 skill 补这个结构洞。镜像 edge_sweep 的骨架。
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from core.config_loader import load_config

_cfg = load_config() or {}
_fleet_cfg = _cfg.get('fleet', {})
_acfg = _cfg.get('edge', {}).get('alerts', {}) or {}
_NODES = _fleet_cfg.get('nodes') or ['vps2', 'vps3', 'bwg', 'oracle1', 'vps5']
_REPORT_DIR = os.path.join(_cfg.get('project_root', os.getcwd()), 'data', 'sentinel', 'edge_reports')


@skill(
    name='edge_health',
    description='扫描边缘节点上报心跳, 失联节点(超过 staleness_min 未上报)生成告警。',
    params={},
    tags=['system']
)
def edge_health() -> str:
    staleness = int(_acfg.get('staleness_min', 30)) * 60
    now = time.time()
    stale = []
    for node in _NODES:
        fp = os.path.join(_REPORT_DIR, f"{node}.json")
        if not os.path.exists(fp):
            stale.append(f"🔴 [{node}] 从未上报 (无报告文件)")
            continue
        age = now - os.path.getmtime(fp)
        if age > staleness:
            stale.append(f"🔴 [{node}] 失联 {int(age//60)} 分钟 (阈值 {staleness//60} 分钟)")
    if not stale:
        return f"✅ 全部 {_NODES.__len__()} 个边缘节点心跳正常"
    return "🛡️ Edge 失联告警\n" + "\n".join(stale)

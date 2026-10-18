#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Edge Sentinel 批量安全巡检 (Fleet Audit) (薄入口)"""

from lite_agent.core.skill_engine import skill
from lite_agent.features.network_ops import edge_service


@skill(
    name='fleet_audit',
    description='对所有边缘节点执行批量确定性安全巡检，并发获取网络、进程、会话和日志快照。自动轮询等待（最长7分钟），耗时极少Token。',
    params={},
    tags=['security', 'sysadmin']
)
def fleet_audit() -> str:
    return edge_service.fleet_audit()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Edge 节点失联 sweep (薄入口)"""

from core.skill_engine import skill
from features.network_ops import edge_service


@skill(
    name='edge_health',
    description='扫描边缘节点上报心跳, 失联节点(超过 staleness_min 未上报)生成告警。',
    params={},
    tags=['system']
)
def edge_health() -> str:
    return edge_service.edge_health()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Edge Sentinel 阶段二: 远程只读命令下发 skill (薄入口)"""

from core.skill_engine import skill
from features.network_ops import edge_service

_NODES = edge_service._get_edge_context()["nodes"]


@skill(
    name='edge_cmd',
    description=(
        "向边缘节点下发只读命令并取回结果 (零信任签名通道)。"
        f"可选节点: {', '.join(_NODES)}。"
        "cmd 必须在白名单内 (df/free/w/uptime/vnstat/cat/journalctl/ss/systemctl 等),"
        "禁止管道/重定向/分号。例: edge_cmd(node='oracle1', cmd='cat /var/log/auth.log')。"
        "白名单外高危命令需根私钥离线签名后经 /api/edge_task 上传, 不走本 skill。"
    ),
    params={
        'node': {
            'type': 'string',
            'description': f'目标边缘节点: {" / ".join(_NODES)}',
        },
        'cmd': {
            'type': 'string',
            'description': '只读命令 (须在白名单内), 如 "vnstat -d", "cat /var/log/auth.log", "journalctl -u ssh --since today"',
        },
    },
    tags=['security', 'sysadmin'],
)
def edge_cmd(node: str, cmd: str) -> str:
    return edge_service.edge_cmd(node=node, cmd=cmd)


@skill(
    name='edge_query',
    description='查询 edge_cmd 下发任务的状态与结果 (异步场景: 同步等待超时后用此查询)。',
    params={
        'task_id': {
            'type': 'string',
            'description': 'edge_cmd 返回的 task_id',
        },
    },
    tags=['security', 'sysadmin'],
)
def edge_query(task_id: str) -> str:
    return edge_service.edge_query(task_id=task_id)


@skill(
    name='edge_sweep',
    description='清理 Edge Sentinel 超时任务，将超时未 ack 的任务重新签名重传，超限则标记失联。',
    params={},
    tags=['system']
)
def edge_sweep() -> str:
    return edge_service.edge_sweep()

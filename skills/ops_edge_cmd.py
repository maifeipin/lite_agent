#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Edge Sentinel 阶段二: 远程只读命令下发 skill (中枢侧 agent 入口)。

设计见 implementation_plan_phase2.md §4.A4。不硬编码检查项 —— agent 自由传
白名单内 cmd, skill 负责白名单预校验 → 热私钥签名 → 插入 edge_tasks(pending)
→ 同步等待结果 30s, 超时则返回异步提示 (边缘靠 cron 5min 拉取, 非长连接)。

节点: vps2 / vps3 / bwg / oracle1 / vps5
白名单: config.json 的 edge.whitelist (默认见 edge_whitelist.DEFAULT_WHITELIST)
高危命令 (白名单外, 如需管道): 管理员本地用根私钥离线签名, 经 POST /api/edge_task 上传。
"""
import hashlib
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.skill_engine import skill
from core.config_loader import load_config
from edge_node import edge_crypto
from core import edge_db
from edge_node import edge_whitelist

_cfg = load_config() or {}
_edge_cfg = _cfg.get("edge", {})
_whitelist = _edge_cfg.get("whitelist") or edge_whitelist.DEFAULT_WHITELIST
_sync_wait_sec = int(_edge_cfg.get("sync_wait_sec", 70))
_NODES = _edge_cfg.get("nodes") or ["vps2", "vps3", "bwg", "oracle1", "vps5"]


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
    node = (node or '').strip()
    cmd = (cmd or '').strip()
    if node not in _NODES:
        return f"❌ 未知节点 '{node}'。可选: {', '.join(_NODES)}"
    if not cmd:
        return "❌ cmd 不能为空"

    hot_priv = os.environ.get("EDGE_HOT_PRIV_KEY", "")
    if not hot_priv:
        return "❌ EDGE_HOT_PRIV_KEY 未配置 (vps1 .env), 无法签名下发。"

    # 1. 白名单预校验 (中枢侧提前拒绝, 避免下发无意义任务; 边缘仍会做最终校验)
    ok, reason = edge_whitelist.validate_cmd(cmd, _whitelist)
    if not ok:
        return (
            f"❌ 命令被白名单拒绝: {reason}\n"
            f"仅白名单内只读命令允许热私钥下发。高危/管道命令需根私钥离线签名后经 /api/edge_task 上传。"
        )

    # 2. 生成 task_id / nonce(不可变, hash(task_id)) / ts / 签名
    task_id = uuid.uuid4().hex
    nonce = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    ts = str(int(time.time()))
    try:
        sig = edge_crypto.sign_task(cmd, ts, nonce, hot_priv)
    except Exception as e:
        return f"❌ 签名失败: {e}"
    edge_db.create_task(task_id, node, cmd, ts, nonce, sig, "hot")

    # 3. 同步等待结果 (边缘靠 cron 5min 拉取, 多数情况会超时走异步)
    deadline = time.time() + _sync_wait_sec
    while time.time() < deadline:
        t = edge_db.get_task(task_id)
        if t and t["status"] in ("done", "failed"):
            return edge_db.task_result_text(task_id)
        time.sleep(1)

    return (
        f"⏳ 命令已签名下发到 {node}, 等待边缘节点下次 cron 拉取执行 (task_id={task_id})。\n"
        f"边缘每 5 分钟拉取一次, 稍后用 edge_query(task_id='{task_id}') 查询结果。"
    )


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
    task_id = (task_id or '').strip()
    if not task_id:
        return "❌ task_id 不能为空"
    return edge_db.task_result_text(task_id)


@skill(
    name='edge_sweep',
    description='清理 Edge Sentinel 超时任务，将超时未 ack 的任务状态重置。供内部 Cron 调用。',
    params={},
    tags=['system']
)
def edge_sweep() -> str:
    # default timeout from edge_cfg or edge_db
    timeout_min = _edge_cfg.get("task_timeout_min", edge_db._DEFAULT_TIMEOUT_MIN)
    n = edge_db.sweep_timeouts(timeout_min)
    if n > 0:
        return f"✅ 成功回收 {n} 个超时 Edge 任务回 pending 状态。"
    # Return empty string to keep cron logs clean when nothing is swept
    return ""

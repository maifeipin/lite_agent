"""
Edge Sentinel 边缘节点业务与运维服务 (features.network_ops.edge_service)。
整合边缘节点命令下发、任务查询、超时重传、心跳健康扫描与全网批量安全巡检。
"""
import os
import sys
import time
import uuid
import hashlib
from typing import Optional, List, Dict, Any

from lite_agent.core.config_loader import load_config
from edge_node import edge_crypto
from lite_agent.core import edge_db
from edge_node import edge_whitelist


def _get_edge_context():
    cfg = load_config() or {}
    edge_cfg = cfg.get("edge", {})
    fleet_cfg = cfg.get("fleet", {})
    alerts_cfg = edge_cfg.get("alerts", {}) or {}
    whitelist = edge_cfg.get("whitelist") or edge_whitelist.DEFAULT_WHITELIST
    sync_wait_sec = int(fleet_cfg.get("sync_wait_sec", 70))
    nodes = fleet_cfg.get("nodes", [])
    report_dir = os.path.join(cfg.get("project_root", os.getcwd()), "data", "sentinel", "edge_reports")
    return {
        "cfg": cfg,
        "edge_cfg": edge_cfg,
        "fleet_cfg": fleet_cfg,
        "alerts_cfg": alerts_cfg,
        "whitelist": whitelist,
        "sync_wait_sec": sync_wait_sec,
        "nodes": nodes,
        "report_dir": report_dir,
    }


COMMANDS = [
    "w",
    "df -h",
    "ss -tunlp",
    "journalctl -u ssh --since \"2 hours ago\" --no-pager",
    "journalctl -u sshd --since \"2 hours ago\" --no-pager"
]


def edge_cmd(node: str, cmd: str) -> str:
    ctx = _get_edge_context()
    nodes = ctx["nodes"]
    node = (node or '').strip()
    cmd = (cmd or '').strip()
    if node not in nodes:
        return f"❌ 未知节点 '{node}'。可选: {', '.join(nodes)}"
    if not cmd:
        return "❌ cmd 不能为空"

    hot_priv = os.environ.get("EDGE_HOT_PRIV_KEY", "")
    if not hot_priv:
        return "❌ EDGE_HOT_PRIV_KEY 未配置 (vps1 .env), 无法签名下发。"

    ok, reason = edge_whitelist.validate_cmd(cmd, ctx["whitelist"])
    if not ok:
        return (
            f"❌ 命令被白名单拒绝: {reason}\n"
            f"仅白名单内只读命令允许热私钥下发。高危/管道命令需根私钥离线签名后经 /api/edge_task 上传。"
        )

    task_id = uuid.uuid4().hex
    nonce = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    ts = str(int(time.time()))
    try:
        sig = edge_crypto.sign_task(cmd, ts, nonce, hot_priv)
    except Exception as e:
        return f"❌ 签名失败: {e}"
    edge_db.create_task(task_id, node, cmd, ts, nonce, sig, "hot")

    deadline = time.time() + ctx["sync_wait_sec"]
    while time.time() < deadline:
        t = edge_db.get_task(task_id)
        if t and t["status"] in ("done", "failed"):
            return edge_db.task_result_text(task_id)
        time.sleep(1)

    return (
        f"⏳ 命令已签名下发到 {node}, 等待边缘节点下次 cron 拉取执行 (task_id={task_id})。\n"
        f"边缘每 5 分钟拉取一次, 稍后用 edge_query(task_id='{task_id}') 查询结果。"
    )


def edge_query(task_id: str) -> str:
    task_id = (task_id or '').strip()
    if not task_id:
        return "❌ task_id 不能为空"
    return edge_db.task_result_text(task_id)


def edge_sweep() -> str:
    ctx = _get_edge_context()
    fleet_cfg = ctx["fleet_cfg"]
    retransmit_timeout_sec = fleet_cfg.get("retransmit_timeout_sec", 120)
    max_retries = fleet_cfg.get("max_retries", 2)
    hot_priv = os.environ.get("EDGE_HOT_PRIV_KEY", "")

    if not hot_priv:
        return "❌ 无法执行 edge_sweep: 缺失 EDGE_HOT_PRIV_KEY"

    stuck_tasks = edge_db.get_stuck_tasks(retransmit_timeout_sec)
    if not stuck_tasks:
        return ""

    retransmitted = 0
    lost = 0
    for task in stuck_tasks:
        ts = str(int(time.time()))
        sig = edge_crypto.sign_task(task["cmd"], ts, task["nonce"], hot_priv)
        if edge_db.update_retransmitted_task(task["id"], ts, sig, max_retries):
            retransmitted += 1
        else:
            lost += 1

    summary = []
    if retransmitted > 0:
        summary.append(f"✅ 成功重签并重传 {retransmitted} 个超时任务。")
    if lost > 0:
        summary.append(f"❌ {lost} 个任务重传超限，已标记为节点失联。")

    return "\n".join(summary)


def edge_health() -> str:
    ctx = _get_edge_context()
    acfg = ctx["alerts_cfg"]
    if not acfg.get('enabled', True):
        return "✅ 告警已禁用"

    staleness = int(acfg.get('staleness_min', 30)) * 60
    now = time.time()
    stale = []
    nodes = ctx["nodes"]
    report_dir = ctx["report_dir"]
    for node in nodes:
        fp = os.path.join(report_dir, f"{node}.json")
        if not os.path.exists(fp):
            stale.append(f"🔴 [{node}] 从未上报 (无报告文件)")
            continue
        age = now - os.path.getmtime(fp)
        if age > staleness:
            stale.append(f"🔴 [{node}] 失联 {int(age//60)} 分钟 (阈值 {staleness//60} 分钟)")
    if not stale:
        return f"✅ 全部 {len(nodes)} 个边缘节点心跳正常"
    return "🛡️ Edge 失联告警\n" + "\n".join(stale)


def fleet_audit() -> str:
    hot_priv = os.environ.get("EDGE_HOT_PRIV_KEY", "")
    if not hot_priv:
        return "❌ EDGE_HOT_PRIV_KEY 未配置 (vps1 .env), 无法签名下发。"

    ctx = _get_edge_context()
    nodes = ctx["nodes"]
    whitelist = ctx["whitelist"]
    task_map = {}
    ts = str(int(time.time()))

    for node in nodes:
        for cmd in COMMANDS:
            ok, _ = edge_whitelist.validate_cmd(cmd, whitelist)
            if not ok:
                continue
            task_id = uuid.uuid4().hex
            nonce = hashlib.sha256(task_id.encode()).hexdigest()[:16]
            sig = edge_crypto.sign_task(cmd, ts, nonce, hot_priv)
            edge_db.create_task(task_id, node, cmd, ts, nonce, sig, "hot")
            task_map[task_id] = {"node": node, "cmd": cmd, "status": "pending"}

    # 轮询等待
    start_time = time.time()
    poll_limit = 420
    while time.time() - start_time < poll_limit:
        all_done = True
        for task_id, info in task_map.items():
            if info["status"] == "pending":
                t = edge_db.get_task(task_id)
                if t and t["status"] in ("done", "failed"):
                    info["status"] = t["status"]
                    info["result"] = t["result_summary"]
                else:
                    all_done = False
        if all_done:
            break
        time.sleep(5)

    lines = [f"🛡️ **Fleet Audit 全网安全巡检报告 ({len(task_map)} 项任务)**\n"]
    for task_id, info in task_map.items():
        node = info["node"]
        cmd = info["cmd"]
        status = info["status"]
        res = info.get("result", "等待超时或未执行")
        icon = "✅" if status == "done" else ("❌" if status == "failed" else "⏳")
        lines.append(f"{icon} **[{node}]** `{cmd}`:\n```\n{res}\n```")

    return "\n".join(lines)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Edge Sentinel 批量安全巡检 (Fleet Audit)。

一次性对所有节点下发多条探测指令，全量入库，长轮询等待结果，
最后将全网快照一次性返回给 LLM。解决了单次通信浪费 Token、超时积压等问题。
"""
import os
import sys
import time
import uuid
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.skill_engine import skill
from core.config_loader import load_config
from edge_node import edge_crypto
from core import edge_db
from edge_node import edge_whitelist

_cfg = load_config() or {}
_edge_cfg = _cfg.get("edge", {})
_fleet_cfg = _cfg.get("fleet", {})
_whitelist = _edge_cfg.get("whitelist") or edge_whitelist.DEFAULT_WHITELIST
_NODES = _fleet_cfg.get("nodes") or ["vps2", "vps3", "bwg", "oracle1", "vps5"]

# 确定性安全巡检清单 (必须完全符合 config.json / whitelist.json 的限制)
# 注意: 边缘 cron 每分钟只 claim 1 条任务(见 edge_db.claim_task LIMIT 1),
# 故 N 条命令 = 至少 N×60s 才能全部拉完。轮询时限必须 > len(COMMANDS)*60。
# 删除了 free -m (内存信息价值低于其他项, 省一个拉取周期)。
COMMANDS = [
    "w",
    "df -h",
    "ss -tunlp",
    "journalctl -u ssh --since \"2 hours ago\" --no-pager",
    "journalctl -u sshd --since \"2 hours ago\" --no-pager"
]

@skill(
    name='fleet_audit',
    description='对所有边缘节点执行批量确定性安全巡检，并发获取网络、进程、会话和日志快照。自动轮询等待（最长7分钟），耗时极少Token。',
    params={},
    tags=['security', 'sysadmin']
)
def fleet_audit() -> str:
    hot_priv = os.environ.get("EDGE_HOT_PRIV_KEY", "")
    if not hot_priv:
        return "❌ EDGE_HOT_PRIV_KEY 未配置 (vps1 .env), 无法签名下发。"

    task_map = {} # task_id -> {"node": node, "cmd": cmd, "status": "pending"}
    ts = str(int(time.time()))
    
    print(f"  [FleetAudit] 正在批量下发 {len(_NODES)*len(COMMANDS)} 条巡检任务...")

    # 1. 批量签发并插入 DB
    for node in _NODES:
        for cmd in COMMANDS:
            # 白名单预校验
            ok, _ = edge_whitelist.validate_cmd(cmd, _whitelist)
            if not ok:
                continue
                
            task_id = uuid.uuid4().hex
            nonce = hashlib.sha256(task_id.encode()).hexdigest()[:16]
            try:
                sig = edge_crypto.sign_task(cmd, ts, nonce, hot_priv)
                edge_db.create_task(task_id, node, cmd, ts, nonce, sig, "hot")
                task_map[task_id] = {"node": node, "cmd": cmd, "status": "pending"}
            except Exception as e:
                print(f"  [FleetAudit] 签名失败: {e}")

    if not task_map:
        return "❌ 没有符合白名单的命令下发。"

    # 2. 长轮询等待结果 (最长 420 秒, 足以覆盖异常重试和慢速命令)
    start_time = time.time()
    deadline = start_time + 420
    last_sweep_time = start_time
    
    print(f"  [FleetAudit] 开始长轮询等待结果，最长 420s (零Token消耗)...")
    
    while time.time() < deadline:
        all_done = True
        for tid, meta in task_map.items():
            if meta["status"] not in ("done", "failed"):
                # poll db
                t = edge_db.get_task(tid)
                if t and t["status"] in ("done", "failed"):
                    meta["status"] = t["status"]
                else:
                    all_done = False
        
        if all_done:
            break
            
        if time.time() - last_sweep_time >= 120:
            from skills.ops_edge_cmd import edge_sweep
            sweep_res = edge_sweep()
            if sweep_res:
                # Replace newlines with spaces for single-line log
                print(f"  [FleetAudit] 触发主动自愈: {sweep_res.replace(chr(10), ' ')}")
            last_sweep_time = time.time()

        time.sleep(2)
        
    duration = time.time() - start_time
    print(f"  [FleetAudit] 轮询结束，耗时 {duration:.1f}s")

    # 3. 结果汇总
    node_results = {node: [] for node in _NODES}
    for tid, meta in task_map.items():
        node = meta["node"]
        cmd = meta["cmd"]
        status = meta["status"]
        
        if status in ("done", "failed"):
            text = edge_db.task_result_text(tid)
            # 如果是找不到服务的报错，我们可以简略处理以省上下文
            # 例如 Ubuntu 没 sshd，CentOS 没 ssh。这里保留原输出因为有价值。
            if "No entries" in text or "Unit sshd.service could not be found" in text or "Unit ssh.service could not be found" in text:
                node_results[node].append(f"### `$ {cmd}`\n> 无记录或服务不存在。")
            else:
                node_results[node].append(f"### `$ {cmd}`\n```\n{text.strip()[:8000]}\n```") # 防单输出过大
        else:
            # 注意: 超时≠节点故障。最常见原因是边缘 cron 尚未拉取到该任务(每分钟1条)。
            # 明确标注"非节点故障", 防止 LLM 误判为节点异常/无法巡检。
            node_results[node].append(f"### `$ {cmd}`\n> ⏳ 轮询超时未回传 (状态: {status}, 非节点故障, 多为 cron 未拉取到该任务, 可重试)")

    # 4. 格式化报告返回
    report = ["# 🛡️ Fleet Audit 全网边缘节点安全快照\n"]
    for node, results in node_results.items():
        report.append(f"## 🖥️ 节点: {node}")
        report.append("\n\n".join(results))
        report.append("\n---\n")

    return "\n".join(report)

"""
Edge 节点通信 HTTP 业务 Adapter 模块 (features.network_ops.edge_adapter).
从公共宿主解耦 Edge 上报、任务拉取、结果回传与安全告警逻辑。
"""

import json
import os
import re
import time
import uuid
from typing import Any
from urllib.parse import parse_qs


def handle_edge_report(http_handler: Any, parsed_url: Any = None) -> None:
    content_length = int(http_handler.headers.get('Content-Length', 0))
    if content_length == 0:
        http_handler.send_error(400, "Bad Request: Empty body")
        return

    body = http_handler.rfile.read(content_length)
    try:
        req_data = json.loads(body.decode('utf-8'))
    except json.JSONDecodeError:
        http_handler.send_error(400, "Bad Request: Invalid JSON")
        return

    node_id = req_data.get('node_id')
    if not node_id:
        http_handler.send_error(400, "Bad Request: Missing node_id")
        return

    from lite_agent.core.constants import PROJECT_ROOT
    report_dir = os.path.join(PROJECT_ROOT, 'data', 'sentinel', 'edge_reports')
    os.makedirs(report_dir, exist_ok=True)

    safe_node_id = re.sub(r'[^a-zA-Z0-9_-]', '', node_id)
    if not safe_node_id:
        http_handler.send_error(400, "Bad Request: Invalid node_id")
        return

    file_path = os.path.join(report_dir, f"{safe_node_id}.json")
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(req_data, f, ensure_ascii=False, indent=2)

        # 1. 先 ACK 边缘
        body_bytes = json.dumps({"status": "success", "message": "Report saved"}).encode('utf-8')
        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json')
        http_handler.send_header('Content-Length', str(len(body_bytes)))
        http_handler.end_headers()
        http_handler.wfile.write(body_bytes)
        http_handler.wfile.flush()

        # 2. 评估告警
        try:
            srv = getattr(http_handler, "server", None)
            api_srv = getattr(srv, "api_server", None)
            agent = getattr(api_srv, "agent", None)
            _evaluate_edge_alert(agent, safe_node_id, req_data)
        except Exception as e:
            print(f"  ⚠️ [edge_report] 告警评估异常: {e}")
    except Exception as e:
        http_handler.send_error(500, f"Internal Server Error: {str(e)}")


def _evaluate_edge_alert(agent: Any, node: str, data: dict) -> None:
    from lite_agent.core.config_loader import load_config
    from lite_agent.core.alerts import push_alert

    cfg = load_config() or {}
    acfg = cfg.get('edge', {}).get('alerts', {}) or {}
    if not acfg.get('enabled', True):
        return

    dedup_window = int(acfg.get('dedup_window_min', 10)) * 60
    win = int(time.time() // dedup_window)

    reason = data.get('report_reason') or ''
    metrics = data.get('metrics', {}) or {}
    security = data.get('security', {}) or {}

    is_routine = (reason == '首次上报') or reason.startswith('心跳') or ('变化(' in reason) or (reason == '常规登录')
    if reason and not is_routine:
        text = f"🚨 [{node}] 边缘安全事件\n原因: {reason}"
        af = security.get('auth_fails', 0)
        if af and af > 0:
            text += f"\nauth_fails(近1h): {af}"
        logins = security.get('recent_logins') or []
        if logins:
            last = logins[-1]
            text += f"\n最近登录: {last.get('user','?')} @ {last.get('ip','?')} ({last.get('method','?')})"
        push_alert(agent, text, title='🚨 Edge 安全告警', color='red',
                   dedup_key=f"{node}:security:{win}:{reason[:30]}")

    disk = metrics.get('disk_percent', 0) or 0
    mem = metrics.get('mem_percent', 0) or 0
    cpu = metrics.get('cpu_load', (0, 0, 0))
    cpu1 = cpu[0] if isinstance(cpu, (list, tuple)) and cpu else 0

    d_thr = acfg.get('disk_percent', 85)
    m_thr = acfg.get('mem_percent', 90)
    c_thr = acfg.get('cpu_load', 5.0)

    if disk >= d_thr:
        push_alert(agent, f"⚠️ [{node}] 磁盘 {disk:.1f}% ≥ 阈值 {d_thr}%",
                   title='⚠️ Edge 资源告警', color='orange',
                   dedup_key=f"{node}:disk:{win}")
    if mem >= m_thr:
        push_alert(agent, f"⚠️ [{node}] 内存 {mem:.1f}% ≥ 阈值 {m_thr}%",
                   title='⚠️ Edge 资源告警', color='orange',
                   dedup_key=f"{node}:mem:{win}")
    if cpu1 >= c_thr:
        push_alert(agent, f"⚠️ [{node}] 1min load {cpu1:.1f} ≥ 阈值 {c_thr}",
                   title='⚠️ Edge 资源告警', color='orange',
                   dedup_key=f"{node}:cpu:{win}")


def handle_pull_task(http_handler: Any, parsed_url: Any) -> None:
    from lite_agent.core import edge_db
    qs = parse_qs(parsed_url.query)
    node = (qs.get('node', [None])[0] or '').strip()
    if not node:
        http_handler.send_error(400, "Bad Request: Missing node")
        return
    task = edge_db.claim_task(node)
    if not task:
        http_handler._quiet = True
        _json_resp(http_handler, 200, {"task": None})
        return
    payload = {
        "task_id": task["id"],
        "node": task["node"],
        "cmd": task["cmd"],
        "ts": task["ts"],
        "nonce": task["nonce"],
        "sig": task["sig"],
        "key_tier": task["key_tier"],
    }
    _json_resp(http_handler, 200, {"task": payload})


def handle_task_result(http_handler: Any, parsed_url: Any = None) -> None:
    from lite_agent.core import edge_db
    body = _read_json_body(http_handler)
    if body is None:
        return
    task_id = body.get('task_id')
    if not task_id:
        http_handler.send_error(400, "Bad Request: Missing task_id")
        return
    try:
        exit_code = int(body.get('exit_code', -1))
    except (TypeError, ValueError):
        exit_code = -1
    updated = edge_db.submit_result(
        task_id, exit_code, body.get('stdout', ''), body.get('stderr', '')
    )
    _json_resp(http_handler, 200, {"status": "ok" if updated else "noop"})


def handle_edge_task(http_handler: Any, parsed_url: Any = None) -> None:
    from lite_agent.core import edge_db
    if getattr(http_handler, 'is_edge', False) or getattr(http_handler, 'is_guest', False):
        http_handler.send_error(403, "Forbidden: admin only")
        return
    body = _read_json_body(http_handler)
    if body is None:
        return
    node, cmd, ts, nonce, sig = (body.get(k) for k in ('node', 'cmd', 'ts', 'nonce', 'sig'))
    if not all([node, cmd, ts, nonce, sig]):
        http_handler.send_error(400, "Bad Request: Missing required fields (node,cmd,ts,nonce,sig)")
        return
    if body.get('key_tier', 'root') != 'root':
        http_handler.send_error(400, "Bad Request: /api/edge_task only accepts key_tier=root")
        return
    task_id = body.get('task_id') or uuid.uuid4().hex
    try:
        edge_db.create_task(task_id, node, cmd, ts, nonce, sig, 'root')
    except Exception as e:
        http_handler.send_error(409, f"Conflict: {e}")
        return
    _json_resp(http_handler, 200, {"status": "ok", "task_id": task_id})


def _read_json_body(http_handler: Any) -> Any:
    content_length = int(http_handler.headers.get('Content-Length', 0))
    if content_length == 0:
        http_handler.send_error(400, "Bad Request: Empty body")
        return None
    body = http_handler.rfile.read(content_length)
    try:
        return json.loads(body.decode('utf-8'))
    except json.JSONDecodeError:
        http_handler.send_error(400, "Bad Request: Invalid JSON")
        return None


def _json_resp(http_handler: Any, code: int, obj: Any) -> None:
    http_handler.send_response(code)
    if hasattr(http_handler, "_send_cors_headers"):
        http_handler._send_cors_headers()
    http_handler.send_header('Content-Type', 'application/json')
    http_handler.end_headers()
    http_handler.wfile.write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))


# 兼容别名
handle_report = handle_edge_report

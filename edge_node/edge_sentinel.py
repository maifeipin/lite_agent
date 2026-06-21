#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""边缘节点安全探针 (Edge Sentinel) - 阶段二: 上行汇报 + 下行命令执行。

阶段一: cron 每 5 分钟采集系统状态与 SSH 登录快照, POST 到中枢 /api/report。
阶段二: 先 GET /api/pull_task 拉取下发任务 → Ed25519 验签 → 防重放 →
        白名单校验 → subprocess 执行(禁止 shell=True) → POST /api/task_result 回传。

依赖: cryptography (Ed25519 验签)。pip install cryptography 或 apt install python3-cryptography。
本文件与 edge_crypto.py / edge_whitelist.py / whitelist.json 同目录部署到 /opt/edge_sentinel/。

安全设计 (见 implementation_plan_phase2.md §1/§3):
- 验签输入严格 cmd|ts|nonce, 分级公钥 (hot 签白名单只读 / root 签高危)。
- ts ±60s 窗口 (NTP 同步前提) + nonce 本地去重 (executed.db)。
- 白名单全等 + 参数模板, 非 root 签名一律走白名单, 任何 shell 元字符拒绝。
- subprocess(shell=False), 禁管道/重定向 (热私钥不允许)。
"""
import os
import sys
import json
import time
import shlex
import sqlite3
import hashlib
import subprocess
import urllib.request
from datetime import datetime, timedelta

# 让本目录的 edge_crypto / edge_whitelist 可 import (边缘部署同目录)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge_crypto  # noqa: E402
import edge_whitelist  # noqa: E402

NODE_ID = os.environ.get("NODE_ID", "vps_unknown")
API_URL = os.environ.get("API_URL", "http://127.0.0.1:8887/api/report")
EDGE_TOKEN = os.environ.get("EDGE_TOKEN", "")
LOOKBACK = "1 hour ago"  # SSH 登录快照回看窗口

# 阶段二配置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXECUTED_DB = os.path.join(SCRIPT_DIR, "executed.db")
WHITELIST_FILE = os.path.join(SCRIPT_DIR, "whitelist.json")
TS_WINDOW_SEC = 60
EXEC_TIMEOUT = 30


def load_env():
    env_file = os.path.join(SCRIPT_DIR, '.env')
    if os.path.exists(env_file):
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())


# ============================================================
# 阶段一: 系统状态 + SSH 安全采集 (不变)
# ============================================================
def get_cpu_load():
    try: return os.getloadavg()
    except: return (0.0, 0.0, 0.0)

def get_mem_usage():
    try:
        with open('/proc/meminfo', 'r') as f: lines = f.readlines()
        mem_total, mem_available = 0, 0
        for line in lines:
            if line.startswith('MemTotal:'): mem_total = int(line.split()[1])
            elif line.startswith('MemAvailable:'): mem_available = int(line.split()[1])
        if mem_total > 0: return round((mem_total - mem_available) / mem_total * 100, 2)
    except: pass
    return 0.0

def get_disk_usage():
    try:
        st = os.statvfs('/')
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total > 0: return round((total - free) / total * 100, 2)
    except: pass
    return 0.0

def hash_file(path):
    if not os.path.exists(path): return None
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""): h.update(chunk)
        return h.hexdigest()
    except: return None

def _run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""

def _has_journalctl():
    return bool(_run(["journalctl", "-u", "ssh", "--no-pager", "-n", "1"], timeout=5).strip()
                or _run(["journalctl", "-u", "sshd", "--no-pager", "-n", "1"], timeout=5).strip())

def _parse_ssh_lines(text):
    fails = 0
    logins = []
    for line in text.splitlines():
        if "Failed password" in line or "Invalid user" in line:
            fails += 1
            continue
        if "Accepted " in line:
            method = "publickey" if "Accepted publickey" in line else ("password" if "Accepted password" in line else "other")
            ip = None
            user = None
            if " from " in line:
                tail = line.split(" from ", 1)[1]
                ip = tail.split()[0] if tail.split() else None
            if " for " in line:
                seg = line.split(" for ", 1)[1]
                user = seg.split()[0] if seg.split() else None
            tstamp = " ".join(line.split()[:3]) if not line[:4].isdigit() else line.split()[0]
            if ip and user:
                logins.append({"user": user, "ip": ip, "method": method, "time": tstamp})
    return fails, logins[-10:]

def collect_ssh_security():
    if _has_journalctl():
        unit = "ssh" if _run(["systemctl", "list-units", "--type=service", "ssh.service"], timeout=5) else "sshd"
        text = _run(["journalctl", "-u", unit, "--since", LOOKBACK, "--no-pager"], timeout=10)
        if not text:
            text = _run(["journalctl", "-u", "sshd", "--since", LOOKBACK, "--no-pager"], timeout=10)
            unit = "sshd"
        if text:
            fails, logins = _parse_ssh_lines(text)
            return {"auth_fails": fails, "recent_logins": logins, "source": f"journalctl:{unit}"}
    for log_path in ['/var/log/auth.log', '/var/log/secure']:
        if not os.path.exists(log_path):
            continue
        try:
            with open(log_path, 'rb') as f:
                f.seek(0, 2); size = f.tell()
                f.seek(max(0, size - 262144))
                text = f.read().decode('utf-8', errors='ignore')
            now = datetime.now()
            cutoff = now - timedelta(hours=1)
            keep = []
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        mon = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}.get(parts[0])
                        day = int(parts[1]); hh, mm, ss = parts[2].split(':')
                        if mon:
                            ts = datetime(now.year, mon, day, int(hh), int(mm), int(ss))
                            if ts.month > now.month: ts = ts.replace(year=now.year - 1)
                            if ts >= cutoff:
                                keep.append(line)
                                continue
                    except Exception:
                        pass
                if "Accepted " in line or "Failed password" in line:
                    keep.append(line)
            fails, logins = _parse_ssh_lines("\n".join(keep))
            return {"auth_fails": fails, "recent_logins": logins, "source": f"file:{log_path}"}
        except Exception:
            continue
    return {"auth_fails": -1, "recent_logins": [], "source": "none"}


# ============================================================
# 阶段二: 下行命令拉取 / 验签 / 执行 / 回传
# ============================================================
def _http_request(url, method="GET", payload=None, token=None, timeout=15):
    """发起 HTTP 请求, 返回 (status, json_or_None)。"""
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode('utf-8')
            try:
                return res.status, json.loads(body)
            except json.JSONDecodeError:
                return res.status, None
    except Exception as e:
        print(f"  HTTP {method} {url} 失败: {e}")
        return None, None


def _load_whitelist():
    """加载本地白名单 (与中枢 config.edge.whitelist 同步)。无文件则用默认。"""
    if os.path.exists(WHITELIST_FILE):
        try:
            with open(WHITELIST_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"  ⚠️ whitelist.json 解析失败, 用默认: {e}")
    return edge_whitelist.DEFAULT_WHITELIST


def _nonce_db():
    conn = sqlite3.connect(EXECUTED_DB, timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, ts TEXT)")
    return conn


def _nonce_seen(conn, nonce):
    row = conn.execute("SELECT 1 FROM nonces WHERE nonce=?", (nonce,)).fetchone()
    return row is not None


def _nonce_record(conn, nonce):
    conn.execute("INSERT OR IGNORE INTO nonces (nonce, ts) VALUES (?, ?)", (nonce, str(int(time.time()))))
    conn.commit()


def _nonce_cleanup(conn):
    """清理 24h 前的 nonce。"""
    cutoff = str(int(time.time()) - 86400)
    conn.execute("DELETE FROM nonces WHERE ts < ?", (cutoff,))
    conn.commit()


def verify_and_execute(task):
    """对一条 pull 到的任务做验签/防重放/白名单/执行, 返回结果 dict。"""
    task_id = task.get("task_id")
    cmd = task.get("cmd", "")
    ts = task.get("ts", "")
    nonce = task.get("nonce", "")
    sig = task.get("sig", "")
    key_tier = task.get("key_tier", "hot")

    # 1. 选公钥 (hot 走热公钥+白名单; root 走根公钥, 跳过白名单)
    if key_tier == "root":
        pub = os.environ.get("EDGE_ROOT_PUBKEY", "")
        if not pub:
            return {"task_id": task_id, "exit_code": -1, "stdout": "", "stderr": "EDGE_ROOT_PUBKEY 未配置"}
    else:
        pub = os.environ.get("EDGE_HOT_PUBKEY", "")
        if not pub:
            return {"task_id": task_id, "exit_code": -1, "stdout": "", "stderr": "EDGE_HOT_PUBKEY 未配置"}

    # 2. 验签 (cmd|ts|nonce)
    if not edge_crypto.verify_task(cmd, ts, nonce, sig, pub):
        return {"task_id": task_id, "exit_code": -1, "stdout": "", "stderr": "签名校验失败 (cmd|ts|nonce 不匹配或公钥错误)"}

    # 3. ts ±60s 窗口 (NTP 同步前提)
    try:
        if abs(int(time.time()) - int(ts)) > TS_WINDOW_SEC:
            return {"task_id": task_id, "exit_code": -1, "stdout": "", "stderr": f"ts 超出 ±{TS_WINDOW_SEC}s 窗口 (防重放/时钟漂移)"}
    except ValueError:
        return {"task_id": task_id, "exit_code": -1, "stdout": "", "stderr": "ts 格式非法"}

    # 4. nonce 去重
    conn = _nonce_db()
    try:
        if _nonce_seen(conn, nonce):
            return {"task_id": task_id, "exit_code": -1, "stdout": "", "stderr": "nonce 重放 (此任务已执行过)"}
    finally:
        pass  # conn 稍后记录时再用

    # 5. 白名单校验 (仅 hot; root 跳过)
    if key_tier == "hot":
        ok, reason = edge_whitelist.validate_cmd(cmd, _load_whitelist())
        if not ok:
            return {"task_id": task_id, "exit_code": -1, "stdout": "", "stderr": f"白名单拒绝: {reason}"}

    # 6. 执行 (禁止 shell=True; 命令已拒绝所有元字符, shlex.split 安全)
    try:
        args = shlex.split(cmd)
        r = subprocess.run(args, capture_output=True, text=True, timeout=EXEC_TIMEOUT)
        exit_code, stdout, stderr = r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return {"task_id": task_id, "exit_code": -1, "stdout": "", "stderr": f"执行超时 (>{EXEC_TIMEOUT}s)"}
    except Exception as e:
        return {"task_id": task_id, "exit_code": -1, "stdout": "", "stderr": f"执行异常: {e}"}

    # 7. 执行成功后记录 nonce (防重放; 只读命令重试无害, 记录在执行后避免回传失败时误锁)
    try:
        _nonce_record(conn, nonce)
        _nonce_cleanup(conn)
        conn.close()
    except Exception:
        pass

    return {"task_id": task_id, "exit_code": exit_code, "stdout": stdout, "stderr": stderr}


def pull_and_execute():
    """循环拉取并执行任务, 直到无任务。pull 优先于 report。"""
    base = API_URL.rsplit('/api/report', 1)[0]
    pull_url = f"{base}/api/pull_task?node={NODE_ID}"
    result_url = f"{base}/api/task_result"
    count = 0
    while True:
        status, data = _http_request(pull_url, method="GET", token=EDGE_TOKEN, timeout=15)
        if status != 200 or not data:
            break
        task = data.get("task")
        if not task:
            break
        count += 1
        print(f"  ↓ 拉到任务 {task.get('task_id')}: [{task.get('key_tier')}] {task.get('cmd')}")
        result = verify_and_execute(task)
        ok = result.get("exit_code", -1)
        # 回传是关键操作, 失败重试 2 次 (HTTPS 偶发超时会导致任务卡 dispatched)
        for attempt in range(3):
            st, _ = _http_request(result_url, method="POST", payload=result, token=EDGE_TOKEN, timeout=15)
            if st == 200:
                break
            if attempt < 2:
                time.sleep(2)
        print(f"  ↑ 回传 exit={ok}" + (f" stderr={result.get('stderr')[:120]}" if ok != 0 else "") + (f" (回传失败,任务将超时回收)" if st != 200 else ""))
    if count:
        print(f"  本轮执行 {count} 个任务")


def report():
    """阶段一上行汇报。"""
    sec = collect_ssh_security()
    payload = {
        "node_id": NODE_ID,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "metrics": {"cpu_load": get_cpu_load(), "mem_percent": get_mem_usage(), "disk_percent": get_disk_usage()},
        "security": {
            "sshd_hash": hash_file('/etc/ssh/sshd_config'),
            "auth_fails": sec["auth_fails"],
            "recent_logins": sec["recent_logins"],
            "log_source": sec["source"],
        }
    }
    status, data = _http_request(API_URL, method="POST", payload=payload, token=EDGE_TOKEN, timeout=10)
    if status == 200:
        print(f"  ✓ 汇报成功: {data}")
    else:
        print(f"  ✗ 汇报失败: status={status}")


def main():
    load_env()
    global NODE_ID, API_URL, EDGE_TOKEN
    NODE_ID = os.environ.get("NODE_ID", NODE_ID)
    API_URL = os.environ.get("API_URL", API_URL)
    EDGE_TOKEN = os.environ.get("EDGE_TOKEN", EDGE_TOKEN)
    if not EDGE_TOKEN:
        print("Error: EDGE_TOKEN is not set.")
        sys.exit(1)

    print(f"[{datetime.utcnow().isoformat()}Z] Edge Sentinel @ {NODE_ID} 启动")
    try:
        pull_and_execute()   # 阶段二: 先拉取并执行下发任务
    except Exception as e:
        print(f"  ⚠️ pull_and_execute 异常: {e}")
    try:
        report()             # 阶段一: 上行汇报
    except Exception as e:
        print(f"  ⚠️ report 异常: {e}")


if __name__ == '__main__': main()

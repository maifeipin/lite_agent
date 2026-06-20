#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""边缘节点安全探针 (Edge Sentinel) - 阶段一上行汇报。

纯 Python 3 标准库,无第三方依赖。由 cron 每 5 分钟唤起,采集系统状态与
SSH 登录安全快照,POST 到中枢 VPS1 的 /api/report。

安全采集设计要点 (2026-06-21 修订):
- 日志源自适应:优先 journalctl (systemd, 如 Debian12/bwg),回退 auth.log/secure 文件。
- 时间窗口:失败/成功登录只看【最近 1 小时】,避免日志轮转造成的历史残留误报
  (此前读"最后 64KB"会把几天前的爆破残留当成当前威胁)。
- recent_logins:采集最近成功登录的 (user, ip, time, method),这是入侵检测的关键
  ——判断"有没有陌生人进来"看成功登录的 IP 是否陌生,而非失败次数。
"""
import os, sys, json, hashlib, subprocess, urllib.request
from datetime import datetime, timedelta

NODE_ID = os.environ.get("NODE_ID", "vps_unknown")
API_URL = os.environ.get("API_URL", "http://127.0.0.1:8887/api/report")
EDGE_TOKEN = os.environ.get("EDGE_TOKEN", "")
LOOKBACK = "1 hour ago"  # SSH 登录快照回看窗口

def load_env():
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_file):
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())

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
    """跑外部命令,返回 stdout 字符串(失败返回空串)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""

def _has_journalctl():
    """系统是否有 systemd journal 可查 sshd 日志。"""
    return bool(_run(["journalctl", "-u", "ssh", "--no-pager", "-n", "1"], timeout=5).strip()
                or _run(["journalctl", "-u", "sshd", "--no-pager", "-n", "1"], timeout=5).strip())

def _parse_ssh_lines(text):
    """从 sshd 日志文本里解析失败与成功登录,返回 (fail_count, [recent_logins])。
    recent_logins 每条: {"user","ip","time","method"},最多保留最近 10 条。"""
    fails = 0
    logins = []
    for line in text.splitlines():
        if "Failed password" in line or "Invalid user" in line:
            fails += 1
            continue
        if "Accepted " in line:
            # 形如: Jun 20 07:34:06 host sshd[pid]: Accepted publickey for root from 116.149.198.210 port 41734 ssh2
            method = "publickey" if "Accepted publickey" in line else ("password" if "Accepted password" in line else "other")
            # 提取 user ... from IP
            ip = None
            user = None
            if " from " in line:
                tail = line.split(" from ", 1)[1]
                ip = tail.split()[0] if tail.split() else None
            if " for " in line:
                seg = line.split(" for ", 1)[1]
                user = seg.split()[0] if seg.split() else None
            # 时间: 取行首 "Jun 20 07:34:06" ( syslog 格式 ) 或 RFC3339 前缀 ( journalctl --since 输出 )
            tstamp = " ".join(line.split()[:3]) if not line[:4].isdigit() else line.split()[0]
            if ip and user:
                logins.append({"user": user, "ip": ip, "method": method, "time": tstamp})
    return fails, logins[-10:]

def collect_ssh_security():
    """采集 SSH 安全快照: 最近 1 小时的失败登录数 + 最近成功登录记录。
    返回 dict: {auth_fails, recent_logins, source}。source 标明数据来源便于排查。"""
    # 1) 优先 journalctl (systemd 系统, 如 Debian 12)
    if _has_journalctl():
        unit = "ssh" if _run(["systemctl", "list-units", "--type=service", "ssh.service"], timeout=5) else "sshd"
        text = _run(["journalctl", "-u", unit, "--since", LOOKBACK, "--no-pager"], timeout=10)
        if not text:
            text = _run(["journalctl", "-u", "sshd", "--since", LOOKBACK, "--no-pager"], timeout=10)
            unit = "sshd"
        if text:
            fails, logins = _parse_ssh_lines(text)
            return {"auth_fails": fails, "recent_logins": logins, "source": f"journalctl:{unit}"}

    # 2) 回退: auth.log / secure 文件 (按时间窗口过滤, 非尾部字节)
    for log_path in ['/var/log/auth.log', '/var/log/secure']:
        if not os.path.exists(log_path):
            continue
        try:
            # 只读最近 256KB, 再按时间窗口粗过滤 (匹配当月当日)
            with open(log_path, 'rb') as f:
                f.seek(0, 2); size = f.tell()
                f.seek(max(0, size - 262144))
                text = f.read().decode('utf-8', errors='ignore')
            # 粗过滤最近 1 小时: 比对当前时间的小时/日
            now = datetime.now()
            cutoff = now - timedelta(hours=1)
            # syslog 行首如 "Jun 20 07:34:06", 解析年用当前年
            keep = []
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        mon = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}.get(parts[0])
                        day = int(parts[1]); hh, mm, ss = parts[2].split(':')
                        if mon:
                            ts = datetime(now.year, mon, day, int(hh), int(mm), int(ss))
                            # 处理跨年 (12月看到1月日志)
                            if ts.month > now.month: ts = ts.replace(year=now.year - 1)
                            if ts >= cutoff:
                                keep.append(line)
                                continue
                    except Exception:
                        pass
                # 解析失败的行不按时间过滤,保留(避免漏数)
                if "Accepted " in line or "Failed password" in line:
                    keep.append(line)
            fails, logins = _parse_ssh_lines("\n".join(keep))
            return {"auth_fails": fails, "recent_logins": logins, "source": f"file:{log_path}"}
        except Exception:
            continue
    return {"auth_fails": -1, "recent_logins": [], "source": "none"}

def main():
    load_env()
    global NODE_ID, API_URL, EDGE_TOKEN
    NODE_ID = os.environ.get("NODE_ID", NODE_ID)
    API_URL = os.environ.get("API_URL", API_URL)
    EDGE_TOKEN = os.environ.get("EDGE_TOKEN", EDGE_TOKEN)
    if not EDGE_TOKEN:
        print("Error: EDGE_TOKEN is not set.")
        sys.exit(1)

    sec = collect_ssh_security()
    payload = {
        "node_id": NODE_ID,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "metrics": {"cpu_load": get_cpu_load(), "mem_percent": get_mem_usage(), "disk_percent": get_disk_usage()},
        "security": {
            "sshd_hash": hash_file('/etc/ssh/sshd_config'),
            "auth_fails": sec["auth_fails"],            # 最近 1 小时失败登录数
            "recent_logins": sec["recent_logins"],       # 最近成功登录 (user/ip/method/time)
            "log_source": sec["source"],                 # 数据来源, 便于排查
        }
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(API_URL, data=data)
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', f'Bearer {EDGE_TOKEN}')
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            print(f"Success: {res.status} - {res.read().decode('utf-8')}")
    except Exception as e: print(f"Error: {str(e)}")

if __name__ == '__main__': main()

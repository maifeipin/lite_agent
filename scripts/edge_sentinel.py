#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, json, hashlib, urllib.request, urllib.error
from datetime import datetime

NODE_ID = os.environ.get("NODE_ID", "vps_unknown")
API_URL = os.environ.get("API_URL", "http://127.0.0.1:8887/api/report")
EDGE_TOKEN = os.environ.get("EDGE_TOKEN", "")

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

def count_ssh_failures():
    log_files = ['/var/log/auth.log', '/var/log/secure']
    log_path = next((f for f in log_files if os.path.exists(f)), None)
    if not log_path: return -1
    try:
        with open(log_path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))  # 最后 64KB
            tail = f.read().decode('utf-8', errors='ignore')
        return sum(1 for l in tail.splitlines() if 'Failed password' in l or 'Invalid user' in l)
    except: return -1

def main():
    load_env()
    global NODE_ID, API_URL, EDGE_TOKEN
    NODE_ID = os.environ.get("NODE_ID", NODE_ID)
    API_URL = os.environ.get("API_URL", API_URL)
    EDGE_TOKEN = os.environ.get("EDGE_TOKEN", EDGE_TOKEN)
    if not EDGE_TOKEN:
        print("Error: EDGE_TOKEN is not set.")
        sys.exit(1)
    payload = {
        "node_id": NODE_ID,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "metrics": {"cpu_load": get_cpu_load(), "mem_percent": get_mem_usage(), "disk_percent": get_disk_usage()},
        "security": {"sshd_hash": hash_file('/etc/ssh/sshd_config'), "auth_fails": count_ssh_failures()}
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

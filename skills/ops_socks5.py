import sqlite3
import os
import sys
import json
import socket
import time
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill

def get_socks5_db_path() -> str:
    """获取 ai.db 的完整路径（支持环境变量、Windows 路径及项目内部 fallback）。"""
    env_path = os.environ.get("SOCKS5_DB_PATH") or os.environ.get("AI_DB_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    
    default_win = r"C:\app\WebGPT\src\bin\Debug\ai.db"
    if os.path.exists(default_win):
        return default_win
        
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fallback_path = os.path.join(project_root, "data", "ai.db")
    return fallback_path

def _ensure_socks5_schema():
    """确保 proxyserver 数据表存在。"""
    db_path = get_socks5_db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS proxyserver (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT,
                runcmd TEXT,
                servername TEXT,
                clientproxy TEXT,
                memo TEXT
            )
        """)

def get_socks5_proxies(query: str = None) -> list:
    """获取 Socks5 代理列表（可模糊搜索）。"""
    _ensure_socks5_schema()
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if query:
            q = f"%{query.strip()}%"
            cursor.execute("""
                SELECT ID as id, host, runcmd, servername, clientproxy, memo
                FROM proxyserver
                WHERE servername LIKE ? OR host LIKE ? OR runcmd LIKE ? OR clientproxy LIKE ? OR memo LIKE ?
                ORDER BY ID DESC
            """, (q, q, q, q, q))
        else:
            cursor.execute("""
                SELECT ID as id, host, runcmd, servername, clientproxy, memo
                FROM proxyserver
                ORDER BY ID DESC
            """)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def get_socks5_proxy_by_id(proxy_id: int) -> dict:
    """根据 ID 获取单个代理节点。"""
    _ensure_socks5_schema()
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ID as id, host, runcmd, servername, clientproxy, memo
            FROM proxyserver WHERE ID = ?
        """, (proxy_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def add_socks5_proxy(host: str, runcmd: str, servername: str, clientproxy: str = "", memo: str = "") -> int:
    """添加新的 Socks5 代理节点。"""
    _ensure_socks5_schema()
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO proxyserver (host, runcmd, servername, clientproxy, memo)
            VALUES (?, ?, ?, ?, ?)
        """, (host.strip(), runcmd.strip(), servername.strip(), clientproxy.strip(), memo.strip()))
        conn.commit()
        return cursor.lastrowid

def update_socks5_proxy(proxy_id: int, host: str = None, runcmd: str = None, servername: str = None, clientproxy: str = None, memo: str = None) -> bool:
    """更新 Socks5 代理节点。"""
    _ensure_socks5_schema()
    db_path = get_socks5_db_path()
    current = get_socks5_proxy_by_id(proxy_id)
    if not current:
        return False
    
    new_host = host.strip() if host is not None else current["host"]
    new_runcmd = runcmd.strip() if runcmd is not None else current["runcmd"]
    new_servername = servername.strip() if servername is not None else current["servername"]
    new_clientproxy = clientproxy.strip() if clientproxy is not None else current["clientproxy"]
    new_memo = memo.strip() if memo is not None else current["memo"]

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE proxyserver
            SET host = ?, runcmd = ?, servername = ?, clientproxy = ?, memo = ?
            WHERE ID = ?
        """, (new_host, new_runcmd, new_servername, new_clientproxy, new_memo, proxy_id))
        conn.commit()
        return cursor.rowcount > 0

def delete_socks5_proxy(proxy_id: int) -> bool:
    """删除 Socks5 代理节点。"""
    _ensure_socks5_schema()
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM proxyserver WHERE ID = ?", (proxy_id,))
        conn.commit()
        return cursor.rowcount > 0

from urllib.parse import urlsplit

LOCAL_PORT_EXCLUDES = {1080, 1081, 1082, 7890, 7891, 8080, 8887, 8888, 18988}

def parse_host_port(host_str: str) -> tuple:
    """从 host 字符串解析 (clean_host, port)，支持 IPv4、IPv6 (带中括号) 及域名。"""
    if not host_str:
        return "", None
    s = host_str.strip()
    if s.startswith("["):
        m = re.match(r'^\[([^\]]+)\](?::(\d+))?$', s)
        if m:
            clean = m.group(1)
            port = int(m.group(2)) if m.group(2) else None
            return clean, port
    elif ":" in s and s.count(":") == 1:
        h, p = s.split(":", 1)
        if p.isdigit():
            return h, int(p)
    return s, None

def extract_proxy_port(runcmd: str = "", host: str = "") -> int:
    """从 host 或 runcmd 命令行中提取代理服务端的真实连接端口，精准防护 URL userinfo 密码干扰并排除本地监听端口。"""
    # 1. 优先从 host 解析显式端口
    _, host_port = parse_host_port(host)
    if host_port and host_port not in LOCAL_PORT_EXCLUDES:
        return host_port

    if runcmd:
        # 2. 剥离本地监听配置子串 (-l, --listen, --socks5, -L, --local)
        clean_cmd = re.sub(r'(?:--listen|-l|-L|--socks5|--local)\s*(?:=\s*|\s+)\S+', '', runcmd)
        
        # 3. 搜寻服务端 URL 配置 (-proxy=, -s, -F, --server)
        server_matches = re.findall(r'(?:--proxy=|-s\s+|-F\s+|--server\s+)(?:[^\s]+)', clean_cmd)
        target_tokens = server_matches if server_matches else [clean_cmd]
        
        for token in target_tokens:
            # 清理前缀 flag 如 --proxy=, -s 
            url_str = re.sub(r'^(?:--proxy=|-s|-F|--server)\s*', '', token).strip()
            if "://" not in url_str and not url_str.startswith("//"):
                url_str = "proxy://" + url_str
            try:
                parsed = urlsplit(url_str)
                if parsed.port and parsed.port not in LOCAL_PORT_EXCLUDES:
                    return parsed.port
                if parsed.scheme in ("https", "wss") and 443 not in LOCAL_PORT_EXCLUDES:
                    return 443
                if parsed.scheme in ("http", "ws") and 80 not in LOCAL_PORT_EXCLUDES:
                    return 80
            except Exception:
                pass
                
        # 4. Fallback 显式端口正则匹配（排除已知本地监听端口）
        for p_str in re.findall(r':(\d{2,5})', " ".join(target_tokens)):
            p = int(p_str)
            if p not in LOCAL_PORT_EXCLUDES and 1 <= p <= 65535:
                return p

    return 4431  # 默认 NaiveProxy 服务端常用端口

def test_socks5_host(host: str, runcmd: str = "", port: int = None, timeout: float = 3.0) -> dict:
    """测试主机 TCP 端口连通性及延迟（支持 IPv4/IPv6 双栈回退与端口自动解析）。"""
    if not host:
        return {"success": False, "error": "Empty host", "latency_ms": -1}
    
    clean_host, host_port = parse_host_port(host)
    target_port = port
    if target_port is None or target_port <= 0:
        target_port = host_port or extract_proxy_port(runcmd=runcmd, host=host)
    
    start_time = time.time()
    try:
        addr_info = socket.getaddrinfo(clean_host, target_port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if not addr_info:
            return {"success": False, "host": clean_host, "port": target_port, "error": "DNS resolution failed", "latency_ms": -1}
        
        last_error = None
        for family, socktype, proto, canonname, sockaddr in addr_info:
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(timeout)
                result = sock.connect_ex(sockaddr)
                latency = round((time.time() - start_time) * 1000, 1)
                sock.close()
                if result == 0:
                    return {"success": True, "host": clean_host, "port": target_port, "latency_ms": latency}
                else:
                    last_error = f"Port {target_port} unreachable (code {result})"
            except Exception as se:
                last_error = str(se)
        
        return {"success": False, "host": clean_host, "port": target_port, "error": last_error or "Connection failed", "latency_ms": -1}
    except Exception as e:
        return {"success": False, "host": clean_host, "port": target_port, "error": str(e), "latency_ms": -1}

def generate_ps1_script(proxy: dict) -> str:
    """生成 Windows PowerShell (.ps1) 检查安装与启动脚本。"""
    servername = proxy.get("servername", "Socks5 Proxy")
    host = proxy.get("host", "")
    runcmd = proxy.get("runcmd", "")
    
    bin_name = "naive"
    if "brook" in runcmd.lower():
        bin_name = "brook"
    
    script = f"""# ========================================================
# Windows Client Script for {servername} ({host})
# Generated by Lite Agent
# ========================================================

Write-Host "[*] Checking dependency '{bin_name}'..." -ForegroundColor Cyan
if (-not (Get-Command '{bin_name}' -ErrorAction SilentlyContinue)) {{
    Write-Host "[!] Warning: '{bin_name}' was not found in PATH." -ForegroundColor Yellow
    Write-Host "    Please ensure '{bin_name}.exe' is installed or present in current folder." -ForegroundColor Yellow
}} else {{
    Write-Host "[+] Found '{bin_name}' executable." -ForegroundColor Green
}}

Write-Host "[*] Starting Socks5 Proxy Node: {servername}..." -ForegroundColor Cyan
Write-Host "    Command: {runcmd}" -ForegroundColor Gray

{runcmd}
"""
    return script

def generate_sh_script(proxy: dict) -> str:
    """生成 Linux/macOS Shell (.sh) 检查安装与启动脚本。"""
    servername = proxy.get("servername", "Socks5 Proxy")
    host = proxy.get("host", "")
    runcmd = proxy.get("runcmd", "")
    
    bin_name = "naive"
    if "brook" in runcmd.lower():
        bin_name = "brook"
        
    script = f"""#!/usr/bin/env bash
# ========================================================
# Linux/macOS Client Script for {servername} ({host})
# Generated by Lite Agent
# ========================================================

CMD="{runcmd}"
BIN_NAME="{bin_name}"

echo -e "\\033[36m[*] Checking dependency '$BIN_NAME'...\\033[0m"
if ! command -v "$BIN_NAME" &> /dev/null; then
    echo -e "\\033[33m[!] Warning: '$BIN_NAME' was not found in PATH.\\033[0m"
    echo -e "\\033[33m    Please install $BIN_NAME or add it to PATH.\\033[0m"
else
    echo -e "\\033[32m[+] Found '$BIN_NAME' executable.\\033[0m"
fi

echo -e "\\033[36m[*] Starting Socks5 Proxy Node: {servername}...\\033[0m"
echo -e "\\033[90m    Command: $CMD\\033[0m"

eval "$CMD"
"""
    return script

# ── Agent Skills ──
@skill(name="socks5_list", description="列出所有 Socks5 代理节点配置（支持关键词过滤）")
def ops_socks5_list(query: str = "") -> str:
    """列出所有 Socks5 代理节点配置（支持关键词过滤）。"""
    proxies = get_socks5_proxies(query)
    if not proxies:
        return "未找到匹配的 Socks5 代理节点。"
    
    lines = [f"| ID | 节点名称 | 服务器 IP/Host | 客户端代理 | 备注 |", "|---|---|---|---|---|"]
    for p in proxies:
        lines.append(f"| {p['id']} | {p['servername']} | `{p['host']}` | `{p['clientproxy']}` | {p['memo']} |")
    return "\n".join(lines)

@skill(name="socks5_add", description="新增 Socks5 代理节点配置")
def ops_socks5_add(servername: str, host: str, runcmd: str = "", clientproxy: str = "", memo: str = "") -> str:
    """新增 Socks5 代理节点配置。"""
    if not servername or not host:
        return "错误：节点名称(servername)和主机地址(host)为必填项。"
    new_id = add_socks5_proxy(host, runcmd, servername, clientproxy, memo)
    return f"成功添加 Socks5 节点 [ID: {new_id}] {servername} ({host})"

@skill(name="socks5_update", description="更新已有的 Socks5 代理节点配置")
def ops_socks5_update(id: int, servername: str = None, host: str = None, runcmd: str = None, clientproxy: str = None, memo: str = None) -> str:
    """更新已有的 Socks5 代理节点配置。"""
    ok = update_socks5_proxy(id, host, runcmd, servername, clientproxy, memo)
    if ok:
        return f"成功更新 Socks5 节点 [ID: {id}]"
    return f"更新失败：未找到 ID 为 {id} 的节点。"

@skill(name="socks5_delete", description="删除指定的 Socks5 代理节点配置")
def ops_socks5_delete(id: int) -> str:
    """删除指定的 Socks5 代理节点配置。"""
    ok = delete_socks5_proxy(id)
    if ok:
        return f"成功删除 Socks5 节点 [ID: {id}]"
    return f"删除失败：未找到 ID 为 {id} 的节点。"

@skill(name="socks5_test", description="测试指定 Socks5 节点的服务器 TCP 连通性")
def ops_socks5_test(id: int) -> str:
    """测试指定 Socks5 节点的服务器 TCP 连通性。"""
    proxy = get_socks5_proxy_by_id(id)
    if not proxy:
        return f"测试失败：未找到 ID 为 {id} 的节点。"
    res = test_socks5_host(host=proxy["host"], runcmd=proxy.get("runcmd", ""))
    if res["success"]:
        return f"节点 [{proxy['servername']}] (端口 {res['port']}) 连通正常，延迟: {res['latency_ms']} ms"
    return f"节点 [{proxy['servername']}] (端口 {res['port']}) 无法连接：{res.get('error', '未知错误')}"


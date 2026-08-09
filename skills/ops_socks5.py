import sqlite3
import os
import sys
import json
import socket
import time
import re
import shlex
import subprocess
import threading
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill

LOCAL_PORT_EXCLUDES = {1080, 1081, 1082, 7890, 7891, 8080, 8887, 8888, 18988}
_socks5_lock = threading.Lock()
_failover_backoff_until = 0
_failover_worker_started = False
_consecutive_failures = 0

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
    """确保 proxyserver 数据表存在，并幂等增加 is_active 和 priority 列 (S-7)。"""
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
        # 检查并幂等增加 is_active 和 priority 列
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(proxyserver)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'is_active' not in columns:
            cursor.execute("ALTER TABLE proxyserver ADD COLUMN is_active INTEGER DEFAULT 0")
        if 'priority' not in columns:
            cursor.execute("ALTER TABLE proxyserver ADD COLUMN priority INTEGER DEFAULT 0")
        conn.commit()

def get_socks5_proxies(query: str = None) -> list:
    """获取 Socks5 代理列表（支持模糊搜索，按 is_active、priority 及 ID 排序）。"""
    _ensure_socks5_schema()
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if query:
            q = f"%{query.strip()}%"
            cursor.execute("""
                SELECT ID as id, host, runcmd, servername, clientproxy, memo, COALESCE(is_active, 0) as is_active, COALESCE(priority, 0) as priority
                FROM proxyserver
                WHERE servername LIKE ? OR host LIKE ? OR runcmd LIKE ? OR clientproxy LIKE ? OR memo LIKE ?
                ORDER BY is_active DESC, priority DESC, ID DESC
            """, (q, q, q, q, q))
        else:
            cursor.execute("""
                SELECT ID as id, host, runcmd, servername, clientproxy, memo, COALESCE(is_active, 0) as is_active, COALESCE(priority, 0) as priority
                FROM proxyserver
                ORDER BY is_active DESC, priority DESC, ID DESC
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
            SELECT ID as id, host, runcmd, servername, clientproxy, memo, COALESCE(is_active, 0) as is_active, COALESCE(priority, 0) as priority
            FROM proxyserver WHERE ID = ?
        """, (proxy_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_current_active_proxy() -> dict:
    """获取当前标记为生效的主节点。"""
    _ensure_socks5_schema()
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ID as id, host, runcmd, servername, clientproxy, memo, COALESCE(is_active, 0) as is_active, COALESCE(priority, 0) as priority
            FROM proxyserver WHERE is_active = 1 LIMIT 1
        """)
        row = cursor.fetchone()
        return dict(row) if row else None

def add_socks5_proxy(host: str, runcmd: str, servername: str, clientproxy: str = "", memo: str = "", priority: int = 0) -> int:
    """添加新的 Socks5 代理节点。"""
    _ensure_socks5_schema()
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO proxyserver (host, runcmd, servername, clientproxy, memo, priority)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (host.strip(), runcmd.strip(), servername.strip(), clientproxy.strip(), memo.strip(), priority))
        conn.commit()
        return cursor.lastrowid

def update_socks5_proxy(proxy_id: int, host: str = None, runcmd: str = None, servername: str = None, clientproxy: str = None, memo: str = None, priority: int = None) -> bool:
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
    new_priority = int(priority) if priority is not None else current["priority"]

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE proxyserver
            SET host = ?, runcmd = ?, servername = ?, clientproxy = ?, memo = ?, priority = ?
            WHERE ID = ?
        """, (new_host, new_runcmd, new_servername, new_clientproxy, new_memo, new_priority, proxy_id))
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

# ── 转换与 Helper 函数 ──
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

def parse_runcmd_to_naive_config(runcmd: str, host: str = "", current_listen: str = "socks://127.0.0.1:18988") -> dict:
    """
    使用 shlex 准确解析 runcmd 命令行参数，并转换为 /etc/proxy/config.json 格式 (P-1, R-1)。
    保持 listen 为宿主不变量 (R-1)，只更新 proxy 与 host-resolver-rules。
    """
    try:
        tokens = shlex.split(runcmd)
    except Exception:
        tokens = runcmd.split()
        
    proxy_val = ""
    rules_val = ""
    
    for token in tokens:
        if token.startswith("--proxy="):
            proxy_val = token.split("=", 1)[1]
        elif token.startswith("--host-resolver-rules="):
            rules_val = token.split("=", 1)[1]
            
    if not rules_val and proxy_val and host:
        try:
            domain = urlsplit(proxy_val).hostname
            if domain and host and domain != host:
                rules_val = f"MAP {domain} {host}"
        except Exception:
            pass
            
    return {
        "listen": current_listen or "socks://127.0.0.1:18988",
        "proxy": proxy_val,
        "host-resolver-rules": rules_val,
        "log": ""
    }

def apply_active_proxy_to_vps1(proxy_id: int) -> tuple[bool, str]:
    """
    将指定节点应用为 VPS1 的生效主节点 (P-2, P-4, B-1, B-2, S-1, S-6)。
    具备特权隔离、安全校验、原子备份、5s短轮询校验、故障自动回滚及 DB 状态一致性保证。
    """
    global _failover_backoff_until, _consecutive_failures
    proxy = get_socks5_proxy_by_id(proxy_id)
    if not proxy:
        return False, f"未找到 ID 为 {proxy_id} 的节点"
        
    runcmd = proxy.get("runcmd", "")
    # 校验是否为 Naive 协议节点 (P-2 Guard)
    is_naive = "naive" in runcmd.lower() or "--proxy=" in runcmd.lower()
    if not is_naive:
        return False, f"节点 [{proxy.get('servername')}] 为 Brook 协议，不可应用为 VPS1 的 Naive 服务主节点"

    with _socks5_lock:
        # 非 POSIX 环境（如 Windows 开发机）直接更新数据库标记并返回提醒
        if os.name != 'posix':
            _update_db_is_active(proxy_id)
            _failover_backoff_until = 0
            _consecutive_failures = 0
            return True, f"开发环境提醒：已更新数据库标记为 active，跳过 Linux 服务重启"

        # 读取宿主环境现网 config.json 提取 current_listen (S-6)
        config_path = "/etc/proxy/config.json"
        current_listen = "socks://127.0.0.1:18988"
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    old_cfg = json.load(f)
                    current_listen = old_cfg.get("listen", current_listen)
            except Exception:
                pass

        config_dict = parse_runcmd_to_naive_config(runcmd, proxy.get("host", ""), current_listen)
        
        # 预校验 JSON 语法正确性 (S-3)
        try:
            json_str = json.dumps(config_dict, indent=2)
            json.loads(json_str)
        except Exception as e:
            return False, f"生成 JSON 格式异常: {str(e)}"

        # 写入临时文件
        tmp_json_path = "/tmp/naive_new_config.json"
        try:
            with open(tmp_json_path, 'w', encoding='utf-8') as f:
                f.write(json_str)
        except Exception as e:
            # 若在 Windows 开发机上无法写 /tmp，更新数据库标记并给出开发环境提醒
            _update_db_is_active(proxy_id)
            _failover_backoff_until = 0
            _consecutive_failures = 0
            return True, f"开发环境提醒：已更新数据库标记为 active，跳过 Linux 服务重启 ({str(e)})"

        # 寻找特权切换 Helper 脚本 (S-1)
        helper_path = "/usr/local/sbin/switch_naive_config.sh"
        if not os.path.exists(helper_path):
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            helper_path = os.path.join(project_root, "scripts", "switch_naive_config.sh")
            
        if not os.path.exists(helper_path):
            _update_db_is_active(proxy_id)
            _failover_backoff_until = 0
            _consecutive_failures = 0
            return True, "已更新数据库主节点标记（当前节点已设为 Active）"

        # 执行 Helper 脚本 (备份->原子替换->重启->5s短轮询校验->自动回滚)
        try:
            is_root = hasattr(os, 'geteuid') and os.geteuid() == 0  # B-2 跨平台安全守卫
            cmd = [helper_path, tmp_json_path] if is_root else ["sudo", helper_path, tmp_json_path]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            
            if proc.returncode == 0:
                # Helper 执行成功后再更新 DB is_active 状态 (B-1 修复)
                _update_db_is_active(proxy_id)
                _failover_backoff_until = 0
                _consecutive_failures = 0
                return True, f"成功切至主节点 [{proxy.get('servername')}] 并已重启 VPS1 代理服务"
            else:
                # Helper 失败已自动回滚系统配置，保持 DB is_active 仍为原节点 (B-1 修复)
                err_msg = proc.stderr or proc.stdout
                return False, f"切换代理服务失败已自动恢复原服务: {err_msg.strip()}"
        except Exception as e:
            return False, f"执行 Helper 脚本异常: {str(e)}"

def _update_db_is_active(active_id: int):
    """更新数据库中 is_active 状态。"""
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE proxyserver SET is_active = CASE WHEN ID = ? THEN 1 ELSE 0 END", (active_id,))
        conn.commit()

def extract_proxy_port(runcmd: str = "", host: str = "") -> int:
    """从 host 或 runcmd 命令行中提取代理服务端的真实连接端口，排除本地监听端口。"""
    _, host_port = parse_host_port(host)
    if host_port and host_port not in LOCAL_PORT_EXCLUDES:
        return host_port

    if runcmd:
        clean_cmd = re.sub(r'(?:--listen|-l|-L|--socks5|--local)\s*(?:=\s*|\s+)\S+', '', runcmd)
        server_matches = re.findall(r'(?:--proxy=|-s\s+|-F\s+|--server\s+)(?:[^\s]+)', clean_cmd)
        target_tokens = server_matches if server_matches else [clean_cmd]
        
        for token in target_tokens:
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
                
        for p_str in re.findall(r':(\d{2,5})', " ".join(target_tokens)):
            p = int(p_str)
            if p not in LOCAL_PORT_EXCLUDES and 1 <= p <= 65535:
                return p

    return 4431

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

def test_socks5_outbound_http(proxy_url: str = "socks5://127.0.0.1:18988", test_url: str = "https://www.google.com", timeout: int = 5) -> dict:
    """
    通过本地 Socks5 代理发起真实 HTTP Over Socks5 网页访问测试 (P-9, R-4)。
    使用系统 curl 命令做零依赖精准测试，校验 HTTP 状态码 %{http_code}。
    """
    start_time = time.time()
    # 防范假在线，校验 HTTP 状态码 200/204/301/302 (R-4)
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--socks5-hostname", "127.0.0.1:18988", "-m", str(timeout), test_url]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout + 2)
        latency_ms = round((time.time() - start_time) * 1000, 1)
        http_code = proc.stdout.strip()
        if http_code in ("200", "204", "301", "302"):
            return {"success": True, "http_code": http_code, "latency_ms": latency_ms, "url": test_url}
        else:
            return {"success": False, "http_code": http_code, "latency_ms": -1, "error": f"HTTP response status {http_code}"}
    except Exception as e:
        return {"success": False, "http_code": "000", "latency_ms": -1, "error": str(e)}

# ── Auto-Failover 后台守护逻辑 (P-5, R-5, R-6, B-4) ──
def failover_worker_loop():
    """后台 Auto-Failover Worker 定时探针与自动故障转移。"""
    global _consecutive_failures, _failover_backoff_until
    failed_round_node_ids = set()  # 记录本轮已失败的节点集合，防止 A<=>B 乒乓无休止切换 (B-4 修复)
    print("  [*] Socks5 Auto-Failover Worker daemon started (polling every 3m)")
    
    while True:
        try:
            time.sleep(180) # 每 3 分钟轮询
            
            # 若处于退避冷却期，跳过 (R-6)
            if time.time() < _failover_backoff_until:
                continue

            # 探测当前 127.0.0.1:18988 出站健康
            res = test_socks5_outbound_http()
            if res["success"]:
                _consecutive_failures = 0
                failed_round_node_ids.clear()
            else:
                _consecutive_failures += 1
                current_active = get_current_active_proxy()
                curr_id = current_active["id"] if current_active else -1
                if curr_id != -1:
                    failed_round_node_ids.add(curr_id)
                    
                print(f"  [!] Socks5 outbound probe failed [{_consecutive_failures}/2]: {res.get('error')}")
                
                # 连续 2 次探针失败触发自动回退
                if _consecutive_failures >= 2:
                    # 查找尚未在本轮标记失败、且优先级最高的备用 Naive 节点 (B-4, P-5, R-5)
                    all_proxies = get_socks5_proxies()
                    candidate = None
                    for p in all_proxies:
                        runcmd = p.get("runcmd", "").lower()
                        is_naive = "naive" in runcmd or "--proxy=" in runcmd
                        if is_naive and p["id"] not in failed_round_node_ids:
                            candidate = p
                            break
                            
                    if candidate:
                        print(f"  [*] Triggering Auto-Failover: switching to [{candidate['servername']}]")
                        ok, msg = apply_active_proxy_to_vps1(candidate["id"])
                        if ok:
                            try:
                                from skills.ops_todo import todo_add
                                todo_add(title=f"Socks5 自动回退告警", description=f"原节点失效，已自动切至备用节点 [{candidate['servername']}]", kind="misc")
                            except Exception:
                                pass
                    else:
                        print("  [!] Warning: All Naive proxy nodes failed in this round. Entering 15m backoff...")
                        _failover_backoff_until = time.time() + 900 # 15 分钟退避
                        _consecutive_failures = 0
                        failed_round_node_ids.clear()
        except Exception as e:
            print(f"  [!] Failover Worker error: {str(e)}")

def start_failover_worker():
    """启动后台 Auto-Failover 线程 (S-5)。"""
    global _failover_worker_started
    if _failover_worker_started:
        return
    _failover_worker_started = True
    t = threading.Thread(target=failover_worker_loop, daemon=True, name="Socks5FailoverWorker")
    t.start()

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
    
    lines = [f"| ID | 状态 | 节点名称 | 服务器 IP/Host | 客户端代理 | 备注 |", "|---|---|---|---|---|---|"]
    for p in proxies:
        active_tag = "🟢 主节点" if p.get("is_active") == 1 else "⚪ 备用"
        lines.append(f"| {p['id']} | {active_tag} | {p['servername']} | `{p['host']}` | `{p['clientproxy']}` | {p['memo']} |")
    return "\n".join(lines)

@skill(name="socks5_add", description="新增 Socks5 代理节点配置")
def ops_socks5_add(servername: str, host: str, runcmd: str = "", clientproxy: str = "", memo: str = "", priority: int = 0) -> str:
    """新增 Socks5 代理节点配置。"""
    if not servername or not host:
        return "错误：节点名称(servername)和主机地址(host)为必填项。"
    new_id = add_socks5_proxy(host, runcmd, servername, clientproxy, memo, priority)
    return f"成功添加 Socks5 节点 [ID: {new_id}] {servername} ({host})"

@skill(name="socks5_set_active", description="将指定的 Socks5 代理节点切换为 VPS1 的当前生效主节点")
def ops_socks5_set_active(id: int) -> str:
    """将指定的 Socks5 代理节点切换为 VPS1 的当前生效主节点。"""
    ok, msg = apply_active_proxy_to_vps1(id)
    return msg

@skill(name="socks5_get_active", description="获取 VPS1 当前生效的 Socks5 代理主节点信息")
def ops_socks5_get_active() -> str:
    """获取 VPS1 当前生效的 Socks5 代理主节点信息。"""
    active = get_current_active_proxy()
    if not active:
        return "当前未选择生效的 Socks5 主节点。"
    return f"VPS1 当前主节点 [ID: {active['id']}] {active['servername']} ({active['host']})"

@skill(name="socks5_update", description="更新已有的 Socks5 代理节点配置")
def ops_socks5_update(id: int, servername: str = None, host: str = None, runcmd: str = None, clientproxy: str = None, memo: str = None, priority: int = None) -> str:
    """更新已有的 Socks5 代理节点配置。"""
    ok = update_socks5_proxy(id, host, runcmd, servername, clientproxy, memo, priority)
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

@skill(name="socks5_test_outbound", description="测试 VPS1 本地代理端口 (127.0.0.1:18988) 的真实 HTTP 翻墙出站能力")
def ops_socks5_test_outbound() -> str:
    """测试 VPS1 本地代理端口 (127.0.0.1:18988) 的真实 HTTP 翻墙出站能力。"""
    res = test_socks5_outbound_http()
    if res["success"]:
        return f"VPS1 本地 18988 代理出站访问正常 (HTTP {res['http_code']})，端到端延迟: {res['latency_ms']} ms"
    return f"VPS1 本地 18988 代理出站访问异常: {res.get('error')}"


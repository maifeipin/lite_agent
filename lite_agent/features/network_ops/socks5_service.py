"""
Network Ops Socks5 代理服务 (features.network_ops.socks5_service).
提供 Socks5 代理节点管理、VPS1 现网 NaiveProxy 切换、探针与 Failover Worker 守护。
为原 skills/ops_socks5.py 的 canonical 业务实现。
"""

import os
import re
import time
import json
import shlex
import socket
import sqlite3
import threading
import subprocess
from urllib.parse import urlsplit
from typing import Optional, Callable, Tuple, Any, Dict, List, Set

from lite_agent.core.config_loader import load_config

from lite_agent.core.constants import PROJECT_ROOT

_cfg = load_config() or {}
_socks5_cfg = _cfg.get("socks5", {}) if isinstance(_cfg, dict) else {}
_default_db = _socks5_cfg.get("db_path") or os.path.join(PROJECT_ROOT, "data", "ai.db")

LOCAL_PORT_EXCLUDES = {1080, 1081, 1082, 7890, 7891, 8080, 8887, 8888, 18988}

_socks5_lock = threading.Lock()
_failover_worker_lock = threading.Lock()
_failover_worker_started = False
_failover_worker_thread: Optional[threading.Thread] = None
_failover_stop_event: Optional[threading.Event] = None
_failover_worker_owners: int = 0
_consecutive_failures = 0
_failover_backoff_until: float = 0.0


def get_socks5_db_path() -> str:
    """获取当前配置的 Socks5 SQLite 数据库路径。"""
    env_path = os.environ.get("SOCKS5_DB_PATH") or os.environ.get("AI_DB_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    default_win = r"C:\app\WebGPT\src\bin\Debug\ai.db"
    if os.path.exists(default_win):
        return default_win
    cfg = load_config() or {}
    s_cfg = cfg.get("socks5", {}) if isinstance(cfg, dict) else {}
    p = s_cfg.get("db_path") or _default_db
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    return p


def _ensure_socks5_schema() -> None:
    """初始化 proxyserver 数据库表结构。"""
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS proxyserver (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT NOT NULL,
                runcmd TEXT,
                servername TEXT,
                clientproxy TEXT,
                memo TEXT,
                priority INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 0
            )
        """)
        cursor.execute("PRAGMA table_info(proxyserver)")
        cols = [c[1] for c in cursor.fetchall()]
        if "is_active" not in cols:
            cursor.execute("ALTER TABLE proxyserver ADD COLUMN is_active INTEGER DEFAULT 0")
        if "priority" not in cols:
            cursor.execute("ALTER TABLE proxyserver ADD COLUMN priority INTEGER DEFAULT 0")
        conn.commit()


def _normalize_proxy_dict(d: dict) -> dict:
    if d is None:
        return None
    res = dict(d)
    if "ID" in res and "id" not in res:
        res["id"] = res["ID"]
    elif "id" in res and "ID" not in res:
        res["ID"] = res["id"]
    return res


def get_socks5_proxies(query: str = "") -> List[Dict[str, Any]]:
    """查询代理节点列表。"""
    _ensure_socks5_schema()
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if query:
            q = f"%{query.strip()}%"
            cursor.execute("""
                SELECT * FROM proxyserver
                WHERE host LIKE ? OR servername LIKE ? OR memo LIKE ?
                ORDER BY is_active DESC, priority DESC, ID ASC
            """, (q, q, q))
        else:
            cursor.execute("""
                SELECT * FROM proxyserver
                ORDER BY is_active DESC, priority DESC, ID ASC
            """)
        return [_normalize_proxy_dict(dict(row)) for row in cursor.fetchall()]


def get_socks5_proxy_by_id(proxy_id: int) -> Optional[Dict[str, Any]]:
    """根据 ID 获取单个代理节点。"""
    _ensure_socks5_schema()
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM proxyserver WHERE ID = ?", (proxy_id,))
        row = cursor.fetchone()
        return _normalize_proxy_dict(dict(row)) if row else None


def get_current_active_proxy() -> Optional[Dict[str, Any]]:
    """获取当前生效的 active 节点。"""
    _ensure_socks5_schema()
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM proxyserver WHERE is_active = 1 LIMIT 1")
        row = cursor.fetchone()
        return _normalize_proxy_dict(dict(row)) if row else None


def add_socks5_proxy(
    host: str,
    runcmd: str = "",
    servername: str = "",
    clientproxy: str = "",
    memo: str = "",
    priority: int = 0
) -> int:
    """新增 Socks5 代理节点。"""
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


def update_socks5_proxy(
    proxy_id: int,
    host: str = None,
    runcmd: str = None,
    servername: str = None,
    clientproxy: str = None,
    memo: str = None,
    priority: int = None
) -> bool:
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


def parse_runcmd_to_naive_config(
    runcmd: str,
    host: str = "",
    current_listen: str = "socks://127.0.0.1:18988"
) -> dict:
    """
    使用 shlex 准确解析 runcmd 命令行参数，并转换为 /etc/proxy/config.json 格式。
    保持 listen 为宿主不变量，只更新 proxy 与 host-resolver-rules。
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


def _update_db_is_active(active_id: int):
    """更新数据库中 is_active 状态。"""
    db_path = get_socks5_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE proxyserver SET is_active = CASE WHEN ID = ? THEN 1 ELSE 0 END", (active_id,))
        conn.commit()


def apply_active_proxy_to_vps1(proxy_id: int) -> Tuple[bool, str]:
    """
    将指定节点应用为 VPS1 的生效主节点。
    具备特权隔离、安全校验、原子备份、5s短轮询校验、故障自动回滚及 DB 状态一致性保证。
    """
    global _failover_backoff_until, _consecutive_failures
    proxy = get_socks5_proxy_by_id(proxy_id)
    if not proxy:
        return False, f"未找到 ID 为 {proxy_id} 的节点"

    runcmd = proxy.get("runcmd", "")
    # 校验是否为 Naive 协议节点
    is_naive = "naive" in runcmd.lower() or "--proxy=" in runcmd.lower()
    if not is_naive:
        return False, f"节点 [{proxy.get('servername')}] 为 Brook 协议，不可应用为 VPS1 的 Naive 服务主节点"

    with _socks5_lock:
        # 非 POSIX 环境（如 Windows 开发机）直接更新数据库标记并返回提醒
        if os.name != 'posix':
            _update_db_is_active(proxy_id)
            _failover_backoff_until = 0.0
            _consecutive_failures = 0
            return True, f"开发环境提醒：已更新数据库标记为 active，跳过 Linux 服务重启"

        # 读取宿主环境现网 config.json 提取 current_listen
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

        # 预校验 JSON 语法正确性
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
            _update_db_is_active(proxy_id)
            _failover_backoff_until = 0.0
            _consecutive_failures = 0
            return True, f"开发环境提醒：已更新数据库标记为 active，跳过 Linux 服务重启 ({str(e)})"

        # 寻找特权切换 Helper 脚本
        helper_path = "/usr/local/sbin/switch_naive_config.sh"
        if not os.path.exists(helper_path):
            helper_path = os.path.join(PROJECT_ROOT, "scripts", "switch_naive_config.sh")

        if not os.path.exists(helper_path):
            _update_db_is_active(proxy_id)
            _failover_backoff_until = 0.0
            _consecutive_failures = 0
            return True, "已更新数据库主节点标记（当前节点已设为 Active）"

        # 执行 Helper 脚本 (备份->原子替换->重启->5s短轮询校验->自动回滚)
        try:
            is_root = hasattr(os, 'geteuid') and os.geteuid() == 0
            cmd = [helper_path, tmp_json_path] if is_root else ["sudo", helper_path, tmp_json_path]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)

            if proc.returncode == 0:
                _update_db_is_active(proxy_id)
                _failover_backoff_until = 0.0
                _consecutive_failures = 0
                return True, f"成功切至主节点 [{proxy.get('servername')}] 并已重启 VPS1 代理服务"
            else:
                err_msg = proc.stderr or proc.stdout
                return False, f"切换代理服务失败已自动恢复原服务: {err_msg.strip()}"
        except Exception as e:
            return False, f"执行 Helper 脚本异常: {str(e)}"


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
    """测试主机 TCP 端口连通性及延迟（支持 IPv4/IPv6 双栈回退与端口自动解析）。参数顺序完全保留原签名。"""
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


def _get_free_local_port() -> int:
    """获取一个随机未被占用的本地空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_socks5_outbound_http(
    proxy_url: str = "socks5://127.0.0.1:18988",
    test_url: str = None,
    timeout: int = 5
) -> dict:
    """
    通过指定的 Socks5 本地代理端点发起真实 HTTP 网页访问测试。
    测试数据包真实出站连通性，并提取回显实际出站公网出口 IP 及地理区域。
    """
    start_time = time.time()
    proxy_target = "127.0.0.1:18988"
    if proxy_url:
        m_target = re.search(r'(?:socks5h?://|socks://)?([^/]+)', proxy_url)
        if m_target:
            proxy_target = m_target.group(1)

    endpoints = [test_url] if test_url else [
        "https://cloudflare.com/cdn-cgi/trace",
        "https://api.ipify.org",
        "https://icanhazip.com"
    ]

    last_error = None
    for url in endpoints:
        cmd = ["curl", "-s", "-w", "\n__HTTP_CODE__:%{http_code}", "--socks5-hostname", proxy_target, "-m", str(timeout), url]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout + 2)
            latency_ms = round((time.time() - start_time) * 1000, 1)
            raw_output = proc.stdout.strip()

            http_code = "000"
            body = raw_output
            if "__HTTP_CODE__:" in raw_output:
                parts = raw_output.rsplit("__HTTP_CODE__:", 1)
                body = parts[0].strip()
                http_code = parts[1].strip()

            if http_code in ("200", "204", "301", "302"):
                ip = ""
                loc = ""

                m_ip = re.search(r'(?:^|\n)ip=([^\r\n]+)', body)
                if m_ip:
                    ip = m_ip.group(1).strip()
                else:
                    m_raw_ip = re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b|[0-9a-fA-F:]{4,}', body)
                    if m_raw_ip:
                        ip = m_raw_ip.group(0).strip()

                m_loc = re.search(r'(?:^|\n)loc=([^\r\n]+)', body)
                if m_loc:
                    loc = m_loc.group(1).strip()

                return {
                    "success": True,
                    "http_code": http_code,
                    "ip": ip,
                    "loc": loc,
                    "latency_ms": latency_ms,
                    "url": url,
                    "proxy_target": proxy_target
                }
            else:
                last_error = f"HTTP response status {http_code}"
        except Exception as e:
            last_error = str(e)

    return {
        "success": False,
        "http_code": "000",
        "ip": "",
        "loc": "",
        "error": last_error or "All outbound endpoints failed",
        "latency_ms": -1,
        "proxy_target": proxy_target
    }


def test_socks5_proxy_outbound(proxy_id: int, timeout: int = 5) -> dict:
    """
    对特定的 Socks5/Naive 节点发起真实 HTTP 出站网络与出口 IP 回显测试。
    - 若该节点为当前 Active 节点：直接复用本地 127.0.0.1:18988 发起测试。
    - 若该节点为候选/非活动节点：动态分配随机空闲端口，启动隔离的临时 naive 进程进行独立测试，测试完成后立即销毁，互不干扰且绝不端口冲突。
    """
    proxy = get_socks5_proxy_by_id(proxy_id)
    if not proxy:
        return {"success": False, "error": f"未找到 ID 为 {proxy_id} 的节点", "latency_ms": -1}

    active_proxy = get_current_active_proxy()
    if (active_proxy and active_proxy["id"] == proxy_id) or proxy.get("is_active") == 1:
        return test_socks5_outbound_http(proxy_url="socks5://127.0.0.1:18988", timeout=timeout)

    runcmd = proxy.get("runcmd", "").strip()
    is_naive = "naive" in runcmd.lower() or "--proxy=" in runcmd.lower()
    if not is_naive:
        return {"success": False, "error": "非 Naive 协议节点（如 Brook）暂不支持独立出站探针测试", "latency_ms": -1}

    if os.name != 'posix':
        return {"success": False, "error": "独立候选节点出站探针仅支持 Linux 环境 (Windows 开发环境请直接使用当前生效主节点)", "latency_ms": -1}

    naive_bin = "/etc/proxy/naive"
    if not os.path.exists(naive_bin):
        naive_bin = "/usr/local/bin/naive" if os.path.exists("/usr/local/bin/naive") else "naive"

    free_port = _get_free_local_port()
    temp_listen = f"socks://127.0.0.1:{free_port}"
    config_dict = parse_runcmd_to_naive_config(runcmd, proxy.get("host", ""), temp_listen)

    proc = None
    tmp_json_path = None
    try:
        import tempfile
        tmp_fd, tmp_json_path = tempfile.mkstemp(prefix="naive_test_", suffix=".json")
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(config_dict, f)

        proc = subprocess.Popen(
            [naive_bin, "--config", tmp_json_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        port_ready = False
        for _ in range(15):
            time.sleep(0.1)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                if s.connect_ex(('127.0.0.1', free_port)) == 0:
                    port_ready = True
                    break

        if not port_ready:
            return {"success": False, "error": f"临时 Naive 实例启动失败 (端口 {free_port} 未就绪)", "latency_ms": -1}

        res = test_socks5_outbound_http(proxy_url=f"socks5://127.0.0.1:{free_port}", timeout=timeout)
        return res
    except Exception as e:
        return {"success": False, "error": f"独立节点测试异常: {str(e)}", "latency_ms": -1}
    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
        if tmp_json_path and os.path.exists(tmp_json_path):
            try:
                os.remove(tmp_json_path)
            except Exception:
                pass


def failover_worker_loop(
    stop_event: Optional[threading.Event] = None,
    poll_interval: float = 180.0,
    probe_fn: Optional[Callable[[], dict]] = None,
    apply_fn: Optional[Callable[[int], Tuple[bool, str]]] = None,
    alert_fn: Optional[Callable[..., None]] = None,
    get_proxies_fn: Optional[Callable[[], List[dict]]] = None,
    get_active_fn: Optional[Callable[[], Optional[dict]]] = None,
):
    """后台 Auto-Failover Worker 定时探针与自动故障转移。支持 stop_event 优雅退出与依赖注入。"""
    global _consecutive_failures, _failover_backoff_until
    failed_round_node_ids = set()
    print("  [*] Socks5 Auto-Failover Worker daemon started (polling every 3m)")

    _probe = probe_fn or test_socks5_outbound_http
    _apply = apply_fn or apply_active_proxy_to_vps1
    _get_proxies = get_proxies_fn or get_socks5_proxies
    _get_active = get_active_fn or get_current_active_proxy

    def _do_alert(title: str, text: str, color: str, dedup_key: str):
        if stop_event is not None and stop_event.is_set():
            return
        if alert_fn is not None:
            alert_fn(title=title, text=text, color=color, dedup_key=dedup_key)
        else:
            try:
                from lite_agent.integrations.notifications import send_im_alert
                send_im_alert(title=title, text=text, color=color, dedup_key=dedup_key)
            except Exception as e:
                print(f"  [!] send_im_alert error: {e}")

    while True:
        try:
            if stop_event is not None:
                if stop_event.wait(timeout=poll_interval):
                    break
            else:
                time.sleep(poll_interval)

            if stop_event is not None and stop_event.is_set():
                break

            if time.time() < _failover_backoff_until:
                continue

            res = _probe()
            if res.get("success"):
                _consecutive_failures = 0
                failed_round_node_ids.clear()
            else:
                _consecutive_failures += 1
                current_active = _get_active()
                curr_id = current_active["id"] if current_active else -1
                curr_name = current_active.get("servername", f"节点#{curr_id}") if current_active else "未知节点"
                if curr_id != -1:
                    failed_round_node_ids.add(curr_id)

                print(f"  [!] Socks5 outbound probe failed [{_consecutive_failures}/2]: {res.get('error')}")

                if _consecutive_failures >= 2:
                    all_proxies = _get_proxies()
                    candidate = None
                    for p in all_proxies:
                        runcmd = p.get("runcmd", "").lower()
                        is_naive = "naive" in runcmd or "--proxy=" in runcmd
                        if is_naive and p["id"] not in failed_round_node_ids:
                            candidate = p
                            break

                    if candidate:
                        print(f"  [*] Triggering Auto-Failover: switching from [{curr_name}] to [{candidate['servername']}]")
                        if stop_event is not None and stop_event.is_set():
                            break
                        ok, msg = _apply(candidate["id"])
                        if ok:
                            _do_alert(
                                title="⚠️ Socks5 自动回退成功",
                                text=f"原节点 [{curr_name}] (ID: {curr_id}) 出站失效，已自动切换至备用节点 [{candidate['servername']}] (ID: {candidate['id']})",
                                color="yellow",
                                dedup_key=f"socks5_failover:{curr_id}->{candidate['id']}:{int(time.time() // 600)}"
                            )
                        else:
                            print(f"  [!] Failed to switch to candidate [{candidate['servername']}]: {msg}")
                            failed_round_node_ids.add(candidate["id"])
                            _do_alert(
                                title="🚨 Socks5 自动回退失败",
                                text=f"原节点 [{curr_name}] (ID: {curr_id}) 出站失效，尝试切换至备用节点 [{candidate['servername']}] (ID: {candidate['id']}) 失败: {msg}",
                                color="red",
                                dedup_key=f"socks5_failover_err:{candidate['id']}:{int(time.time() // 300)}"
                            )
                    else:
                        print("  [!] Warning: All Naive proxy nodes failed in this round. Entering 15m backoff...")
                        _failover_backoff_until = time.time() + 900
                        _consecutive_failures = 0
                        failed_round_node_ids.clear()
                        _do_alert(
                            title="🚨 Socks5 全部备用节点耗尽",
                            text=f"原节点 [{curr_name}] (ID: {curr_id}) 出站失效，且本轮所有备用 Naive 节点均探测失败或不可用！系统进入 15 分钟退避冷却，请运维人员尽快介入排查。",
                            color="red",
                            dedup_key=f"socks5_all_exhausted:{int(time.time() // 900)}"
                        )
        except Exception as e:
            print(f"  [!] Failover Worker error: {str(e)}")
            if stop_event is not None and stop_event.is_set():
                break


def _stop_failover_generation(event: threading.Event, timeout: float) -> None:
    """Retain an in-flight worker until it actually exits; fence old handles."""
    global _failover_worker_started, _failover_worker_thread, _failover_stop_event
    global _consecutive_failures, _failover_backoff_until
    with _failover_worker_lock:
        if event is None or _failover_stop_event is not event:
            return
        event.set()
        worker = _failover_worker_thread
    if worker is not None and worker.is_alive():
        if worker is threading.current_thread():
            raise RuntimeError("Worker cannot join itself")
        worker.join(timeout=timeout)
        if worker.is_alive():
            raise TimeoutError(f"Worker did not stop in {timeout}s")
    with _failover_worker_lock:
        if _failover_stop_event is event:
            _failover_worker_started = False
            _failover_worker_thread = None
            _failover_stop_event = None
            _consecutive_failures = 0
            _failover_backoff_until = 0.0


def start_failover_worker(
    poll_interval: float = 180.0,
    probe_fn: Optional[Callable[[], dict]] = None,
    apply_fn: Optional[Callable[[int], Tuple[bool, str]]] = None,
    alert_fn: Optional[Callable[..., None]] = None,
    stop_event: Optional[threading.Event] = None,
    get_proxies_fn: Optional[Callable[[], List[dict]]] = None,
    get_active_fn: Optional[Callable[[], Optional[dict]]] = None,
) -> Callable[[], None]:
    """
    启动后台 Auto-Failover Worker，具备多宿主引用计数与代次隔离。
    返回当前宿主的幂等释放句柄 (stop callback)。
    """
    global _failover_worker_started, _failover_worker_thread, _failover_stop_event, _failover_worker_owners
    with _failover_worker_lock:
        if _failover_worker_thread is not None and _failover_worker_thread.is_alive():
            if _failover_stop_event is not None and _failover_stop_event.is_set():
                raise RuntimeError("旧的 Worker 正在停止中，无法复用或启动新实例")
            _failover_worker_owners += 1
            current_generation_event = _failover_stop_event
            target_event = current_generation_event
            released = False
            def _release_existing():
                nonlocal released
                if released:
                    return
                released = True
                global _failover_worker_owners
                with _failover_worker_lock:
                    if _failover_stop_event is not target_event:
                        return
                    _failover_worker_owners = max(0, _failover_worker_owners - 1)
                    should_stop = (_failover_worker_owners == 0)
                if should_stop:
                    _stop_failover_generation(target_event, timeout=2.0)
            return _release_existing

        _failover_worker_owners = 1
        ev = stop_event if stop_event is not None else threading.Event()
        _failover_stop_event = ev
        _failover_worker_started = True

        def _thread_target():
            try:
                failover_worker_loop(
                    stop_event=ev,
                    poll_interval=poll_interval,
                    probe_fn=probe_fn,
                    apply_fn=apply_fn,
                    alert_fn=alert_fn,
                    get_proxies_fn=get_proxies_fn,
                    get_active_fn=get_active_fn,
                )
            finally:
                pass


        t = threading.Thread(target=_thread_target, daemon=True, name="Socks5FailoverWorker")
        _failover_worker_thread = t
        t.start()

        released = False
        def _release_worker():
            nonlocal released
            if released:
                return
            released = True
            global _failover_worker_owners
            with _failover_worker_lock:
                if _failover_stop_event is not ev:
                    return
                _failover_worker_owners = max(0, _failover_worker_owners - 1)
                should_stop = (_failover_worker_owners == 0)
            if should_stop:
                _stop_failover_generation(ev, timeout=2.0)

        return _release_worker


def stop_failover_worker(timeout: float = 2.0) -> None:
    """强制停止当前运行的 Failover Worker。"""
    global _failover_worker_owners
    with _failover_worker_lock:
        _failover_worker_owners = 0
        event = _failover_stop_event
    if event is not None:
        _stop_failover_generation(event, timeout=timeout)


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

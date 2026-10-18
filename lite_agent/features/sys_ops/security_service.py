"""
Sys Ops 系统安全、Sentinel 扫描与审计业务服务 (features.sys_ops.security_service)。
从 skills/ops_sentinel.py 与 skills/ops_security.py 迁入安全检查与基线对比业务。
"""
import os
import json
import hashlib
import time
import re
import socket
import struct
import subprocess
from datetime import datetime
from collections import Counter
from typing import Optional, List, Dict, Any

from lite_agent.core.constants import PROJECT_ROOT

DATA_DIR = os.path.join(PROJECT_ROOT, 'data', 'sentinel')
BASELINE_FILE = os.path.join(DATA_DIR, 'baseline.json')
STATE_FILE = os.path.join(DATA_DIR, 'state.json')
FINDINGS_FILE = os.path.join(DATA_DIR, 'findings.jsonl')


class Sentinel:
    def __init__(self):
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR, exist_ok=True)
        self.baseline = self._load_baseline()
        self.state = self._load_state()
        self.findings = []
        self.is_baseline_run = not os.path.exists(BASELINE_FILE)

    def _load_baseline(self):
        if os.path.exists(BASELINE_FILE):
            try:
                with open(BASELINE_FILE, 'r') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_baseline(self):
        tmp_file = BASELINE_FILE + '.tmp'
        with open(tmp_file, 'w') as f:
            json.dump(self.baseline, f, indent=2)
        os.replace(tmp_file, BASELINE_FILE)

    def _save_state(self):
        tmp_file = STATE_FILE + '.tmp'
        with open(tmp_file, 'w') as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp_file, STATE_FILE)

    def _should_update_baseline(self):
        return self.is_baseline_run or getattr(self, 'update_baseline_mode', False)

    def _add_finding(self, module, severity, message, details=None):
        finding = {
            "timestamp": datetime.now().isoformat(),
            "module": module,
            "severity": severity,
            "message": message,
            "details": details or {}
        }
        if not self.is_baseline_run and not getattr(self, 'update_baseline_mode', False):
            alert_signature = hashlib.md5((str(module) + str(severity)).encode('utf-8')).hexdigest()
            throttles = self.state.get("throttles", {})
            last_time = throttles.get(alert_signature, 0)
            now = time.time()
            if now - last_time < 4 * 3600:
                return
            throttles = {k: v for k, v in throttles.items() if now - v < 4 * 3600}
            throttles[alert_signature] = now
            self.state["throttles"] = throttles

            self.findings.append(finding)
            try:
                with open(FINDINGS_FILE, 'a') as f:
                    f.write(json.dumps(finding) + "\n")
            except Exception:
                pass

    def _hash_file(self, path):
        if not os.path.exists(path) or os.path.islink(path):
            return None
        try:
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    def _read_lines(self, path):
        if not os.path.exists(path):
            return []
        try:
            with open(path, 'r', errors='ignore') as f:
                return f.readlines()
        except Exception:
            return []

    def scan_ssh_logs(self):
        log_files = ['/var/log/auth.log', '/var/log/secure']
        log_path = next((f for f in log_files if os.path.exists(f)), None)
        if not log_path:
            return
        lines = self._read_lines(log_path)
        try:
            st = os.stat(log_path)
            state_key = "ssh_log_state"
            recent_lines = lines[-2000:]
            failed_attempts = 0
            success_root = 0
            for line in recent_lines:
                if "Failed password" in line:
                    failed_attempts += 1
                elif "Accepted password" in line:
                    if re.search(r'\broot\b', line):
                        success_root += 1
            self.state[state_key] = {"inode": st.st_ino, "size": st.st_size}
            if failed_attempts > 100:
                self._add_finding("ssh", "medium", f"High volume of failed SSH logins ({failed_attempts} attempts)")
            if success_root > 0:
                self._add_finding("ssh", "high", f"Root login via password detected ({success_root} times)")
        except Exception:
            pass

    def scan_user_drift(self):
        passwd_lines = self._read_lines('/etc/passwd')
        users = {}
        for line in passwd_lines:
            parts = line.strip().split(':')
            if len(parts) >= 3:
                users[parts[0]] = parts[2]
                if parts[2] == '0' and parts[0] != 'root':
                    self._add_finding("user", "high", f"Non-root user with UID 0: {parts[0]}")

        baseline_users = self.baseline.get('users', {})
        new_users = set(users.keys()) - set(baseline_users.keys())
        if new_users and not self.is_baseline_run:
            self._add_finding("user", "medium", f"New users detected: {', '.join(new_users)}")

        if self.is_baseline_run or getattr(self, 'update_baseline_mode', False):
            self.baseline['users'] = users

        group_lines = self._read_lines('/etc/group')
        target_groups = {'sudo', 'wheel', 'docker'}
        group_members = {}
        for line in group_lines:
            parts = line.strip().split(':')
            if len(parts) >= 4 and parts[0] in target_groups:
                members = parts[3].split(',') if parts[3] else []
                group_members[parts[0]] = [m for m in members if m]

        baseline_groups = self.baseline.get('groups', {})
        for g_name, members in group_members.items():
            base_members = set(baseline_groups.get(g_name, []))
            new_members = set(members) - base_members
            if new_members and not self.is_baseline_run:
                self._add_finding("user", "high", f"New members added to highly privileged group '{g_name}': {', '.join(new_members)}")

        if self.is_baseline_run or getattr(self, 'update_baseline_mode', False):
            self.baseline['groups'] = group_members

    def scan_ssh_drift(self):
        ssh_config = '/etc/ssh/sshd_config'
        cfg_hash = self._hash_file(ssh_config)
        base_hash = self.baseline.get('sshd_config_hash')
        if cfg_hash and base_hash and cfg_hash != base_hash and not self.is_baseline_run:
            self._add_finding("ssh_drift", "medium", "sshd_config has been modified")
        if self.is_baseline_run or getattr(self, 'update_baseline_mode', False):
            self.baseline['sshd_config_hash'] = cfg_hash

        auth_keys_hashes = {}
        auth_keys_paths = ['/root/.ssh/authorized_keys', '/root/.ssh/authorized_keys2']
        if os.path.exists('/home'):
            for user_dir in os.listdir('/home'):
                user_ssh_dir = os.path.join('/home', user_dir, '.ssh')
                if not os.path.normpath(user_ssh_dir).startswith('/home'):
                    continue
                auth_keys_paths.append(os.path.join(user_ssh_dir, 'authorized_keys'))
                auth_keys_paths.append(os.path.join(user_ssh_dir, 'authorized_keys2'))

        for path in auth_keys_paths:
            if os.path.exists(path):
                if os.path.islink(path):
                    target = os.readlink(path)
                    if any(bad in target for bad in ['/dev/null', '/tmp', '/dev/shm']):
                        self._add_finding("ssh_drift", "high", f"authorized_keys symlinked to suspicious target: {path} -> {target}")
                try:
                    st = os.stat(path)
                    if bool(st.st_mode & 0o022):
                        self._add_finding("ssh_drift", "medium", f"authorized_keys is group/other writable: {path}")
                except Exception:
                    pass
                auth_keys_hashes[path] = self._hash_file(path)

        base_keys = self.baseline.get('authorized_keys', {})
        for path, h in auth_keys_hashes.items():
            if path in base_keys and base_keys[path] != h and not self.is_baseline_run:
                self._add_finding("ssh_drift", "high", f"authorized_keys modified: {path}")

        if self.is_baseline_run or getattr(self, 'update_baseline_mode', False):
            self.baseline['authorized_keys'] = auth_keys_hashes

    def scan_persistence(self):
        targets = [
            '/etc/crontab',
            '/etc/ld.so.preload',
            '/etc/profile',
            '/root/.bashrc',
            '/root/.bash_profile'
        ]
        dirs = [
            '/etc/cron.d',
            '/var/spool/cron',
            '/var/spool/cron/crontabs',
            '/etc/systemd/system'
        ]
        for d in dirs:
            if os.path.exists(d):
                for root, _, files in os.walk(d):
                    for f in files:
                        targets.append(os.path.join(root, f))

        persist_hashes = {}
        for t in targets:
            h = self._hash_file(t)
            if h:
                persist_hashes[t] = h

        base_persist = self.baseline.get('persistence', {})
        for t, h in persist_hashes.items():
            if t not in base_persist and not self.is_baseline_run:
                self._add_finding("persistence", "high", f"New persistence anchor created: {t}")
            elif t in base_persist and base_persist[t] != h and not self.is_baseline_run:
                self._add_finding("persistence", "high", f"Persistence anchor modified: {t}")

        if self.is_baseline_run or getattr(self, 'update_baseline_mode', False):
            self.baseline['persistence'] = persist_hashes

    def scan_ports(self):
        ports = []
        try:
            with open('/proc/net/tcp', 'r') as f:
                lines = f.readlines()[1:]
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 4 and parts[3] == '0A':
                        local_addr = parts[1]
                        ip_hex, port_hex = local_addr.split(':')
                        port = int(port_hex, 16)
                        if ip_hex == '00000000':
                            ports.append(port)
            if os.path.exists('/proc/net/tcp6'):
                with open('/proc/net/tcp6', 'r') as f:
                    lines = f.readlines()[1:]
                    for line in lines:
                        parts = line.split()
                        if len(parts) >= 4 and parts[3] == '0A':
                            local_addr = parts[1]
                            ip_hex, port_hex = local_addr.split(':')
                            port = int(port_hex, 16)
                            if ip_hex == '00000000000000000000000000000000':
                                ports.append(port)
        except Exception:
            pass

        ports = list(set(ports))
        allowed_ports = {22, 80, 443}
        baseline_ports = set(self.baseline.get('public_ports', []))

        for p in ports:
            if p not in allowed_ports and p not in baseline_ports and not self.is_baseline_run:
                self._add_finding("network", "high", f"New public listening port detected: {p}")

        if self._should_update_baseline():
            self.baseline['public_ports'] = list(set(ports))

    def check_ports(self):
        return self.scan_ports()

    def scan_processes(self):
        suspicious_paths = ['/tmp/', '/var/tmp/', '/dev/shm/', '/run/']
        known_miners = ['xmrig', 'masscan', 'zmap', 'kdevtmpfsi', 'kinsing']

        try:
            pids = [pid for pid in os.listdir('/proc') if pid.isdigit()]
        except Exception:
            pids = []

        for pid in pids:
            try:
                exe_path = os.readlink(f'/proc/{pid}/exe')
                if ' (deleted)' in exe_path:
                    if any(exe_path.startswith(p) for p in suspicious_paths):
                        self._add_finding("process", "high", f"Deleted executable running from suspicious path: {exe_path} (PID: {pid})")
                for sp in suspicious_paths:
                    if exe_path.startswith(sp):
                        self._add_finding("process", "high", f"Process running from suspicious path: {exe_path} (PID: {pid})")
                        break
                name = os.path.basename(exe_path.replace(' (deleted)', ''))
                for m in known_miners:
                    if m in name.lower():
                        self._add_finding("process", "high", f"Known malicious process name detected: {name} (PID: {pid})")
            except Exception:
                pass

    def scan_log_tampering(self):
        logs = ['/var/log/auth.log', '/var/log/secure', '/var/log/wtmp', '/var/log/btmp', '/var/log/lastlog']
        log_states = self.state.get('log_states', {})
        for log in logs:
            if not os.path.exists(log):
                if log in log_states and not self.is_baseline_run:
                    self._add_finding("tampering", "high", f"Core log file disappeared: {log}")
                continue

            if os.path.islink(log):
                target = os.readlink(log)
                if any(bad in target for bad in ['/dev/null', '/tmp', '/dev/shm']):
                    self._add_finding("tampering", "high", f"Log file symlinked to blackhole: {log} -> {target}")

            try:
                st = os.stat(log)
                old_size = log_states.get(log, 0)
                if st.st_size < old_size and old_size > 1024 * 10:
                    if not os.path.exists(f"{log}.1") and not os.path.exists(f"{log}.1.gz"):
                        self._add_finding("tampering", "high", f"Log file truncated suspiciously: {log} (Size: {old_size} -> {st.st_size})")
                log_states[log] = st.st_size
            except Exception:
                pass
        self.state['log_states'] = log_states

    def scan_secret_leaks(self):
        secret_files = ['.env', 'config.json', 'secrets.json']
        for s_file in secret_files:
            file_path = os.path.join(PROJECT_ROOT, s_file)
            if os.path.exists(file_path):
                try:
                    st = os.stat(file_path)
                    if bool(st.st_mode & 0o044):
                        self._add_finding("secret_leak", "medium", f"Sensitive file '{s_file}' is readable by group/others (Mode: {oct(st.st_mode)[-3:]})")
                except Exception:
                    pass

                try:
                    res = subprocess.run(
                        ['git', 'ls-files', '--error-unmatch', s_file],
                        cwd=project_root,
                        capture_output=True,
                        text=True
                    )
                    if res.returncode == 0:
                        self._add_finding("secret_leak", "high", f"CRITICAL: Secret file '{s_file}' is tracked by Git repository!")
                except Exception:
                    pass

        patterns = [
            (r'sk-[a-zA-Z0-9]{32,}', "OpenAI/DeepSeek API Key"),
            (r'AIzaSy[a-zA-Z0-9_-]{33}', "Google API Key"),
            (r'ghp_[a-zA-Z0-9]{36}', "GitHub Personal Access Token"),
            (r'xox[baprs]-[a-zA-Z0-9_-]{10,}', "Slack Token"),
        ]
        exclude_dirs = {'.git', '__pycache__', 'venv', '.venv', 'data', 'node_modules', '.gemini'}

        for root, dirs, files in os.walk(project_root):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for file in files:
                if file in secret_files or file.endswith(('.example.json', '.example.env', '.pyc')):
                    continue
                if file.endswith(('.py', '.sh', '.cs', '.js', '.ts', '.yml', '.yaml')):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, project_root)
                    lines = self._read_lines(full_path)
                    for line_idx, line in enumerate(lines, 1):
                        for pat, desc in patterns:
                            if re.search(pat, line):
                                self._add_finding(
                                    "secret_leak",
                                    "high",
                                    f"Possible hardcoded secret ({desc}) in {rel_path}:{line_idx}"
                                )
                                break

    def run_all(self, update_baseline=False):
        self.update_baseline_mode = update_baseline
        self.scan_ssh_logs()
        self.scan_user_drift()
        self.scan_ssh_drift()
        self.scan_persistence()
        self.scan_ports()
        self.scan_processes()
        self.scan_log_tampering()
        self.scan_secret_leaks()

        if self._should_update_baseline():
            self._save_baseline()

        self._save_state()
        return self.findings

    def run_scan(self, update_baseline=False):
        return self.run_all(update_baseline=update_baseline)


def sentinel_scan(mode: str = 'quick', update_baseline: bool = False) -> str:
    s = Sentinel()
    is_base = s.is_baseline_run
    hostname = socket.gethostname()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    findings = s.run_all(update_baseline=update_baseline or (mode == 'baseline'))

    if is_base or update_baseline:
        return f"🛡️ Sentinel 首次运行：已成功建立安全基线 (Baseline)。未触发告警检测。\n(Host: {hostname} | Time: {now_str})"

    if not findings:
        return f"✅ Sentinel 扫描完成：未发现异常漂移或入侵迹象。\n(Host: {hostname} | Time: {now_str})"

    high_count = sum(1 for f in findings if str(f.get('severity', '')).lower() == 'high')
    med_count = sum(1 for f in findings if str(f.get('severity', '')).lower() == 'medium')

    msg = f"⚠️ Sentinel 扫描完成：发现 {len(findings)} 个异常！(High: {high_count}, Medium: {med_count})\n(Host: {hostname} | Time: {now_str})\n\n"
    for f in findings:
        sev = str(f.get('severity', '')).upper()
        msg += f"- [{sev}] [{f.get('module')}] {f.get('message')}\n"

    return msg


def sentinel_baseline(action: str = 'show', confirm: str = '') -> str:
    if action == 'show':
        if not os.path.exists(BASELINE_FILE):
            return "❌ 尚未建立基线。"
        try:
            with open(BASELINE_FILE, 'r') as f:
                data = json.load(f)
            return f"📊 当前基线数据总览:\n- 监控用户数: {len(data.get('users', {}))}\n- 监控授权 Key: {len(data.get('authorized_keys', {}))}\n- 监控持久化锚点: {len(data.get('persistence', {}))}\n- 暴露监听端口: {data.get('public_ports', [])}"
        except Exception:
            return "❌ 读取基线失败。"
    elif action == 'refresh':
        if confirm != 'YES':
            return "⚠️ 刷新基线会接受当前所有状态为合法（含潜在的入侵后门）。请传 confirm='YES' 确认。"
        s = Sentinel()
        s.run_all(update_baseline=True)
        return "🔄 已强制刷新基线，当前系统状态将被视为【合法】。"
    return "无效的操作。"


def sentinel_findings(hours: int = 24, min_severity: str = 'medium', limit: Optional[int] = None) -> str:
    if not os.path.exists(FINDINGS_FILE):
        return "没有找到任何告警记录。"

    results = []
    cutoff = time.time() - (hours * 3600)
    severities = ['medium', 'high'] if str(min_severity).lower() == 'medium' else ['high']

    try:
        with open(FINDINGS_FILE, 'r') as f:
            for line in f:
                if not line.strip():
                    continue
                doc = json.loads(line)
                dt_str = doc.get('timestamp')
                dt = datetime.fromisoformat(dt_str) if dt_str else datetime.now()
                sev = str(doc.get('severity', '')).lower()
                if dt.timestamp() >= cutoff and sev in severities:
                    results.append(doc)
    except Exception:
        return "❌ 读取 findings 失败。"

    if limit is not None and limit > 0:
        results = results[-limit:]

    if not results:
        return f"✅ 过去 {hours} 小时内没有 {min_severity} 及以上级别的告警。"

    msg = f"🔍 过去 {hours} 小时内发现 {len(results)} 条告警:\n"
    for r in results:
        sev = str(r.get('severity', '')).upper()
        msg += f"[{r.get('timestamp')}] [{sev}] [{r.get('module')}] {r.get('message')}\n"
    return msg


def ops_security_audit(hours: int = 24) -> str:
    sections = []
    try:
        hostname = socket.gethostname()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        hostname, ip = "(获取失败)", "(获取失败)"

    sections.append(f"=== 主机信息 ===\n主机名: {hostname}\n内网IP: {ip}\n")
    sections.append("=== 最近10次成功登录 ===")
    try:
        r = subprocess.run(["last", "-n", "10", "-a"], capture_output=True, text=True, timeout=10)
        sections.append(r.stdout.strip() or r.stderr.strip() or '(无结果)')
    except Exception as e:
        sections.append(f'(错误: {e})')

    sections.append("\n=== 最近失败登录尝试 ===")
    try:
        r = subprocess.run(["lastb", "-n", "20"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0 and "permission denied" in r.stderr.lower():
            sections.append('(需要root权限或无失败记录)')
        else:
            sections.append(r.stdout.strip() or r.stderr.strip() or '(无结果)')
    except Exception as e:
        sections.append(f'(错误: {e})')

    # 3. SSH 爆破统计
    sections.append("\n=== SSH 失败来源 IP TOP10 ===")
    auth_log = '/var/log/auth.log'
    if os.path.exists(auth_log):
        try:
            failed_ips = []
            pattern = re.compile(r'Failed\s+password\s+for\s+.*?\s+from\s+(\S+)')
            with open(auth_log, 'r', errors='ignore') as f:
                for line in f:
                    m = pattern.search(line)
                    if m:
                        failed_ips.append(m.group(1))
            if failed_ips:
                counts = Counter(failed_ips).most_common(10)
                lines = [f"{count:7d} {ip}" for ip, count in counts]
                sections.append('\n'.join(lines))
            else:
                sections.append('(无失败记录)')
        except Exception as e:
            sections.append(f'(无法读取 auth.log: {e})')
    else:
        secure_log = '/var/log/secure'
        if os.path.exists(secure_log):
            try:
                failed_ips = []
                pattern = re.compile(r'Failed\s+password\s+for\s+.*?\s+from\s+(\S+)')
                with open(secure_log, 'r', errors='ignore') as f:
                    for line in f:
                        m = pattern.search(line)
                        if m:
                            failed_ips.append(m.group(1))
                if failed_ips:
                    counts = Counter(failed_ips).most_common(10)
                    lines = [f"{count:7d} {ip}" for ip, count in counts]
                    sections.append('\n'.join(lines))
                else:
                    sections.append('(无失败记录)')
            except Exception as e:
                sections.append(f'(无法读取 secure: {e})')
        else:
            sections.append('(无法读取 auth.log/secure 或无失败记录)')

    # 4. 当前连接
    sections.append("\n=== 当前在线用户 ===")
    try:
        r = subprocess.run(["who"], capture_output=True, text=True, timeout=10)
        sections.append(r.stdout.strip() or r.stderr.strip() or '(无结果)')
    except Exception as e:
        sections.append(f'(错误: {e})')

    return "\n".join(sections)

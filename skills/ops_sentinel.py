import os
import json
import hashlib
import time
import re
import socket
import struct
from datetime import datetime

# In the actual script, we'll import skill from skill_engine
from core.skill_engine import skill

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'sentinel')
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
        if not self.is_baseline_run and not getattr(self, 'update_baseline_mode', False):
            # 引入静默期 (Throttling)，4小时内同 module+severity 不重复推送
            # 签名只用稳定维度，不含动态 message（如计数值），否则 SSH 暴力破解等
            # 计数递增场景每次都会生成新签名，导致静默失效
            alert_signature = hashlib.md5((module + severity).encode('utf-8')).hexdigest()
            throttles = self.state.get("throttles", {})
            last_time = throttles.get(alert_signature, 0)
            now = time.time()
            
            if now - last_time < 4 * 3600:
                return  # 静默期内，跳过此告警

            # 清理过期条目，防止 throttles 字典无限增长
            throttles = {k: v for k, v in throttles.items() if now - v < 4 * 3600}

            throttles[alert_signature] = now
            self.state["throttles"] = throttles

            self.findings.append({
                "timestamp": datetime.now().isoformat(),
                "module": module,
                "severity": severity,
                "message": message,
                "details": details or {}
            })

    def _hash_file(self, path):
        if not os.path.exists(path) or os.path.islink(path):
            return None
        try:
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    h.update(chunk)
            return h.hexdigest()
        except:
            return None

    def _read_lines(self, path):
        if not os.path.exists(path):
            return []
        try:
            with open(path, 'r', errors='ignore') as f:
                return f.readlines()
        except:
            return []

    # ---------------------------------------------------------
    # Module 1: SSH Login & Brute Force
    # ---------------------------------------------------------
    def scan_ssh_logs(self):
        log_files = ['/var/log/auth.log', '/var/log/secure']
        log_path = next((f for f in log_files if os.path.exists(f)), None)
        if not log_path:
            return

        lines = self._read_lines(log_path)
        # To avoid reading the whole file every time, we should track inode & size
        try:
            st = os.stat(log_path)
            state_key = "ssh_log_state"
            last_state = self.state.get(state_key, {"inode": 0, "size": 0})
            
            start_idx = 0
            if st.st_ino == last_state.get("inode") and st.st_size >= last_state.get("size", 0):
                # Approximation: we just read from the end. In a real script we'd seek.
                # Since this is a simple script, we'll approximate line count or byte offset.
                pass 
                
            # For MVP, we just count occurrences of "Failed password" and "Accepted password" in the last 1000 lines
            recent_lines = lines[-2000:]
            failed_attempts = 0
            success_root = 0
            success_pwd = 0
            for line in recent_lines:
                if "Failed password" in line:
                    failed_attempts += 1
                elif "Accepted password" in line:
                    success_pwd += 1
                    if re.search(r'\broot\b', line):
                        success_root += 1
            
            # Update state
            self.state[state_key] = {"inode": st.st_ino, "size": st.st_size}
            
            # Brute force threshold
            if failed_attempts > 100:
                self._add_finding("ssh", "medium", f"High volume of failed SSH logins ({failed_attempts} attempts)")
            if success_root > 0:
                self._add_finding("ssh", "high", f"Root login via password detected ({success_root} times)")
        except Exception as e:
            pass

    # ---------------------------------------------------------
    # Module 2: User & Group Drift
    # ---------------------------------------------------------
    def scan_user_drift(self):
        passwd_lines = self._read_lines('/etc/passwd')
        users = {}
        for line in passwd_lines:
            parts = line.strip().split(':')
            if len(parts) >= 3:
                users[parts[0]] = parts[2] # username: uid
                
                # Check non-root UID 0
                if parts[2] == '0' and parts[0] != 'root':
                    self._add_finding("user", "high", f"Non-root user with UID 0: {parts[0]}")

        # Check new users against baseline
        baseline_users = self.baseline.get('users', {})
        new_users = set(users.keys()) - set(baseline_users.keys())
        if new_users and not self.is_baseline_run:
            self._add_finding("user", "medium", f"New users detected: {', '.join(new_users)}")
            
        if self.is_baseline_run or getattr(self, 'update_baseline_mode', False):
            self.baseline['users'] = users

        # Group drift
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

    # ---------------------------------------------------------
    # Module 3: SSH Key & Config Drift
    # ---------------------------------------------------------
    def scan_ssh_drift(self):
        ssh_config = '/etc/ssh/sshd_config'
        cfg_hash = self._hash_file(ssh_config)
        base_hash = self.baseline.get('sshd_config_hash')
        if cfg_hash and base_hash and cfg_hash != base_hash and not self.is_baseline_run:
            self._add_finding("ssh_drift", "medium", "sshd_config has been modified")
        if self.is_baseline_run or getattr(self, 'update_baseline_mode', False):
            self.baseline['sshd_config_hash'] = cfg_hash
        
        # Scan authorized_keys
        auth_keys_hashes = {}
        # root
        auth_keys_paths = ['/root/.ssh/authorized_keys', '/root/.ssh/authorized_keys2']
        # other users
        for user_dir in os.listdir('/home'):
            user_ssh_dir = os.path.join('/home', user_dir, '.ssh')
            if not os.path.normpath(user_ssh_dir).startswith('/home'):
                continue
            auth_keys_paths.append(os.path.join(user_ssh_dir, 'authorized_keys'))
            auth_keys_paths.append(os.path.join(user_ssh_dir, 'authorized_keys2'))
            
        for path in auth_keys_paths:
            if os.path.exists(path):
                # Check softlink
                if os.path.islink(path):
                    target = os.readlink(path)
                    if any(bad in target for bad in ['/dev/null', '/tmp', '/dev/shm']):
                        self._add_finding("ssh_drift", "high", f"authorized_keys symlinked to suspicious target: {path} -> {target}")
                
                # Check permissions
                st = os.stat(path)
                if bool(st.st_mode & 0o022): # group or other writable
                    self._add_finding("ssh_drift", "medium", f"authorized_keys is group/other writable: {path}")
                    
                auth_keys_hashes[path] = self._hash_file(path)
                
        base_keys = self.baseline.get('authorized_keys', {})
        for path, h in auth_keys_hashes.items():
            if path in base_keys and base_keys[path] != h and not self.is_baseline_run:
                self._add_finding("ssh_drift", "high", f"authorized_keys modified: {path}")
        
        if self.is_baseline_run or getattr(self, 'update_baseline_mode', False):
            self.baseline['authorized_keys'] = auth_keys_hashes

    # ---------------------------------------------------------
    # Module 4: Persistence Anchor Drift
    # ---------------------------------------------------------
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

    # ---------------------------------------------------------
    # Module 5: Public Listening Ports
    # ---------------------------------------------------------
    def scan_ports(self):
        ports = []
        try:
            with open('/proc/net/tcp', 'r') as f:
                lines = f.readlines()[1:]
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 4 and parts[3] == '0A': # TCP_LISTEN
                        local_addr = parts[1]
                        ip_hex, port_hex = local_addr.split(':')
                        port = int(port_hex, 16)
                        if ip_hex == '00000000': # 0.0.0.0
                            ports.append(port)
            # IPv6
            if os.path.exists('/proc/net/tcp6'):
                with open('/proc/net/tcp6', 'r') as f:
                    lines = f.readlines()[1:]
                    for line in lines:
                        parts = line.split()
                        if len(parts) >= 4 and parts[3] == '0A':
                            local_addr = parts[1]
                            ip_hex, port_hex = local_addr.split(':')
                            port = int(port_hex, 16)
                            if ip_hex == '00000000000000000000000000000000': # ::
                                ports.append(port)
        except:
            pass

        ports = list(set(ports))
        allowed_ports = {22, 80, 443}
        baseline_ports = set(self.baseline.get('public_ports', []))
        
        for p in ports:
            if p not in allowed_ports and p not in baseline_ports and not self.is_baseline_run:
                self._add_finding("network", "high", f"New public listening port detected: {p}")
                
        if self._should_update_baseline():
            self.baseline['public_ports'] = list(set(ports))

    # ---------------------------------------------------------
    # Module 6: Suspicious Processes
    # ---------------------------------------------------------
    def scan_processes(self):
        suspicious_paths = ['/tmp/', '/var/tmp/', '/dev/shm/', '/run/']
        known_miners = ['xmrig', 'masscan', 'zmap', 'kdevtmpfsi', 'kinsing']
        
        pids = [pid for pid in os.listdir('/proc') if pid.isdigit()]
        for pid in pids:
            try:
                exe_path = os.readlink(f'/proc/{pid}/exe')
                
                # Check if deleted
                if ' (deleted)' in exe_path:
                    # Ignore some valid deleted executables, but flag /tmp ones
                    if any(exe_path.startswith(p) for p in suspicious_paths):
                        self._add_finding("process", "high", f"Deleted executable running from suspicious path: {exe_path} (PID: {pid})")
                
                # Check path
                for sp in suspicious_paths:
                    if exe_path.startswith(sp):
                        self._add_finding("process", "high", f"Process running from suspicious path: {exe_path} (PID: {pid})")
                        break
                        
                # Check name
                name = os.path.basename(exe_path.replace(' (deleted)', ''))
                for m in known_miners:
                    if m in name.lower():
                        self._add_finding("process", "high", f"Known malicious process name detected: {name} (PID: {pid})")
            except:
                pass

    # ---------------------------------------------------------
    # Module 7: Core Log Tampering
    # ---------------------------------------------------------
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
            
            st = os.stat(log)
            old_size = log_states.get(log, 0)
            
            # Massive truncation without logrotate (approx heuristic: size dropped by > 90% but no .1 file exists)
            # A bit complex to do perfectly, but we can just check if size shrinks
            if st.st_size < old_size and old_size > 1024 * 10:
                if not os.path.exists(f"{log}.1") and not os.path.exists(f"{log}.1.gz"):
                    self._add_finding("tampering", "high", f"Log file truncated suspiciously: {log} (Size: {old_size} -> {st.st_size})")
                    
            log_states[log] = st.st_size
            
        self.state['log_states'] = log_states

    # ---------------------------------------------------------
    # Module 8: Secret & Environment Leaks
    # ---------------------------------------------------------
    def scan_secret_leaks(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # 1. Check .env, config.json permissions and Git tracking
        secret_files = ['.env', 'config.json', 'secrets.json']
        for s_file in secret_files:
            file_path = os.path.join(project_root, s_file)
            if os.path.exists(file_path):
                # Check permissions (group or other readable)
                st = os.stat(file_path)
                if bool(st.st_mode & 0o044):
                    self._add_finding("secret_leak", "medium", f"Sensitive file '{s_file}' is readable by group/others (Mode: {oct(st.st_mode)[-3:]})")
                
                # Check if file is mistakenly tracked by Git
                try:
                    import subprocess
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

        # 2. Hardcoded secret patterns scanning in code files
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
        
        if self.findings:
            with open(FINDINGS_FILE, 'a') as f:
                for finding in self.findings:
                    f.write(json.dumps(finding) + "\n")
                    
        return self.findings
@skill(
    name='sentinel_scan',
    description="执行轻量级安全扫描 (ops_sentinel)，检测 SSH、用户越权、持久化后门、异常进程等。",
    params={
        'mode': {
            'type': 'string',
            'description': '扫描模式：quick 或 full (当前暂无区别，均执行完整检查)',
            'default': 'quick'
        }
    },
    tags=['security', 'sysadmin']
)
def sentinel_scan(mode='quick') -> str:
    s = Sentinel()
    is_base = s.is_baseline_run
    import socket
    from datetime import datetime
    hostname = socket.gethostname()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    findings = s.run_all()
    
    if is_base:
        return f"🛡️ Sentinel 首次运行：已成功建立安全基线 (Baseline)。未触发告警检测。\n(Host: {hostname} | Time: {now_str})"
    
    if not findings:
        return f"✅ Sentinel 扫描完成：未发现异常漂移或入侵迹象。\n(Host: {hostname} | Time: {now_str})"
        
    high_count = sum(1 for f in findings if f['severity'] == 'high')
    med_count = sum(1 for f in findings if f['severity'] == 'medium')
    
    msg = f"⚠️ Sentinel 扫描完成：发现 {len(findings)} 个异常！(High: {high_count}, Medium: {med_count})\n(Host: {hostname} | Time: {now_str})\n\n"
    for f in findings:
        msg += f"- [{f['severity'].upper()}] [{f['module']}] {f['message']}\n"
    
    return msg

@skill(
    name='sentinel_baseline',
    description="管理安全基线 (Baseline)。允许操作如 'show' (查看) 或 'refresh' (强制刷新，接受所有当前状态为合法漂移)。",
    params={
        'action': {
            'type': 'string',
            'description': '操作类型: show, refresh',
            'default': 'show'
        },
        'confirm': {
            'type': 'string',
            'description': '刷新基线需传 YES 确认',
            'default': ''
        }
    },
    tags=['security', 'sysadmin']
)
def sentinel_baseline(action='show', confirm='') -> str:
    if action == 'show':
        if not os.path.exists(BASELINE_FILE):
            return "❌ 尚未建立基线。"
        try:
            with open(BASELINE_FILE, 'r') as f:
                data = json.load(f)
            return f"📊 当前基线数据总览:\n- 监控用户数: {len(data.get('users', {}))}\n- 监控授权 Key: {len(data.get('authorized_keys', {}))}\n- 监控持久化锚点: {len(data.get('persistence', {}))}\n- 暴露监听端口: {data.get('public_ports', [])}"
        except:
            return "❌ 读取基线失败。"
    elif action == 'refresh':
        if confirm != 'YES':
            return "⚠️ 刷新基线会接受当前所有状态为合法（含潜在的入侵后门）。请传 confirm='YES' 确认。"
        s = Sentinel()
        s.run_all(update_baseline=True)
        return "🔄 已强制刷新基线，当前系统状态将被视为【合法】。"
    return "无效的操作。"

@skill(
    name='sentinel_findings',
    description="查询本地发现的入侵或漂移告警。",
    params={
        'hours': {
            'type': 'integer',
            'description': '查询过去多少小时内的告警',
            'default': 24
        },
        'min_severity': {
            'type': 'string',
            'description': '最低严重级别: medium 或 high',
            'default': 'medium'
        }
    },
    tags=['security', 'sysadmin']
)
def sentinel_findings(hours=24, min_severity='medium') -> str:
    if not os.path.exists(FINDINGS_FILE):
        return "没有找到任何告警记录。"
        
    results = []
    cutoff = time.time() - (hours * 3600)
    severities = ['medium', 'high'] if min_severity == 'medium' else ['high']
    
    try:
        with open(FINDINGS_FILE, 'r') as f:
            for line in f:
                if not line.strip(): continue
                doc = json.loads(line)
                dt = datetime.fromisoformat(doc['timestamp'])
                if dt.timestamp() >= cutoff and doc['severity'] in severities:
                    results.append(doc)
    except:
        return "❌ 读取 findings 失败。"
        
    if not results:
        return f"✅ 过去 {hours} 小时内没有 {min_severity} 及以上级别的告警。"
        
    msg = f"🔍 过去 {hours} 小时内发现 {len(results)} 条告警:\n"
    for r in results:
        msg += f"[{r['timestamp']}] [{r['severity'].upper()}] [{r['module']}] {r['message']}\n"
    return msg

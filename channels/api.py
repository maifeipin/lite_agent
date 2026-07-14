import json
import threading
import time
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from agent import IncomingMessage

class ApiHandler(BaseHTTPRequestHandler):
    """
    统一的开放 API 处理器，处理 HTTP 请求。
    """
    def log_message(self, format, *args):
        if getattr(self, '_quiet', False):
            return
        super().log_message(format, *args)
    
    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')

    def do_OPTIONS(self):
        self.send_response(200, "ok")
        self._send_cors_headers()
        self.end_headers()

    def _auth(self) -> bool:
        import os
        auth_token = self.server.api_server.auth_token
        guest_token = self.server.api_server.config.get("guest_token", "")
        # The edge_token is at the root config, so we can check os.environ directly since it's mapped from .env
        edge_token = os.environ.get("EDGE_TOKEN", "")
        self.is_guest = False
        self.is_edge = False
        
        if not auth_token and not guest_token and not edge_token:
            return True
            
        auth_header = self.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            self.send_error(401, "Unauthorized")
            return False
            
        token = auth_header.split(' ')[1]
        
        if auth_token and token == auth_token:
            self.is_guest = False
            return True
        elif guest_token and token == guest_token:
            self.is_guest = True
            return True
        elif edge_token and token == edge_token:
            self.is_edge = True
            return True
            
        self.send_error(403, "Forbidden")
        return False

    def do_GET(self):
        parsed_url = urlparse(self.path)

        # 仪表盘 API 无需认证（仅返回注册表指令列表，无敏感数据）
        if parsed_url.path == '/api/v1/dashboard':
            self._handle_dashboard()
            return

        if not self._auth():
            return
        
        # 边缘节点权限隔离：仅允许 /api/report, /api/pull_task
        if getattr(self, 'is_edge', False) and parsed_url.path not in ('/api/report', '/api/pull_task'):
            self.send_error(403, "Forbidden: Edge token is limited to /api/report, /api/pull_task")
            return

        if parsed_url.path == '/api/pull_task':
            self._handle_pull_task(parsed_url.query)
        elif parsed_url.path == '/api/v1/sessions':
            self._handle_sessions(parsed_url.query)
        elif parsed_url.path == '/api/v1/task/stream':
            self._handle_task_stream(parsed_url.query)
        elif parsed_url.path == '/api/v1/email/html':
            self._handle_email_html(parsed_url.query)
        elif parsed_url.path == '/api/v1/todos':
            self._handle_todos(parsed_url.query)
        elif parsed_url.path == '/api/v1/dashboard':
            self._handle_dashboard()
        elif parsed_url.path == '/v1/models':
            self._handle_openai_models()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed_url = urlparse(self.path)

        # 登录接口无需认证
        if parsed_url.path == '/api/v1/auth':
            self._handle_auth()
            return

        if not self._auth():
            return
            
        parsed_url = urlparse(self.path)
        
        # 边缘节点权限隔离：仅允许 /api/report, /api/task_result
        if getattr(self, 'is_edge', False) and parsed_url.path not in ('/api/report', '/api/task_result'):
            self.send_error(403, "Forbidden: Edge token is limited to /api/report, /api/task_result")
            return

        if parsed_url.path in ('/api/v1/chat', '/api/v1/task'):
            self._handle_chat_or_task()
        elif parsed_url.path == '/v1/chat/completions':
            self._handle_openai_chat_completions()
        elif parsed_url.path == '/api/v1/dashboard':
            self._handle_dashboard()
        elif parsed_url.path == '/api/report':
            self._handle_edge_report()
        elif parsed_url.path == '/api/task_result':
            self._handle_task_result()
        elif parsed_url.path == '/api/edge_task':
            self._handle_edge_task()
        else:
            self.send_error(404, "Not Found")

    def _handle_chat_or_task(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: Empty body")
            return
            
        body = self.rfile.read(content_length)
        try:
            req_data = json.loads(body.decode('utf-8'))
        except json.JSONDecodeError:
            self.send_error(400, "Bad Request: Invalid JSON")
            return

        session_id = req_data.get('session_id')
        text = req_data.get('text')
        if not session_id or not text:
            self.send_error(400, "Bad Request: Missing session_id or text")
            return

        notify_channels = req_data.get('notify_channels', [])

        msg = IncomingMessage(
            channel='api',
            user_id=session_id,
            chat_id=session_id,
            message_id=str(time.time()),
            text=text,
            notify_channels=notify_channels
        )

        agent = self.server.api_server.agent
        
        # 阻塞调用 agent.handle
        resp = agent.handle(msg)

        if not resp:
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"type": "sync", "status": "completed", "response": ""}).encode('utf-8'))
            return

        self.send_response(200)
        self._send_cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()

        if resp.task_id:
            # 这是一个异步长任务
            out_data = {
                "type": "async",
                "task_id": resp.task_id,
                "message": resp.text
            }
        else:
            # 同步返回
            out_data = {
                "type": "sync",
                "status": "completed",
                "response": resp.text
            }

        self.wfile.write(json.dumps(out_data, ensure_ascii=False).encode('utf-8'))

    def _handle_dashboard(self):
        """返回仪表盘可用的指令列表（来自注册表）。"""
        from core.command_registry import CommandRegistry
        items = CommandRegistry().items_for_dashboard()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(items, ensure_ascii=False).encode('utf-8'))

    def _handle_sessions(self, query: str):
        """返回最近会话记录列表（来源通道、时间、Token量、模型）。"""
        import sqlite3, os
        qs = parse_qs(query)
        limit = int(qs.get("limit", ["30"])[0])
        channel_filter = qs.get("channel", [None])[0]

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "data", "sessions.db")

        if not os.path.exists(db_path):
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"sessions": [], "total": 0}, ensure_ascii=False).encode('utf-8'))
            return

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

            # 总数
            total_row = conn.execute("SELECT COUNT(*) as cnt FROM sessions").fetchone()
            total = total_row["cnt"] if total_row else 0

            # 查询会话，关联最新使用的模型
            sql = """
                SELECT s.session_key, s.status, s.tool_calls,
                       s.token_usage, s.updated_at, s.goal,
                       (SELECT a.model FROM api_usage_log a
                        WHERE a.session_key = s.session_key
                        ORDER BY a.created_at DESC LIMIT 1) as model
                FROM sessions s
            """
            params = []
            if channel_filter:
                sql += " WHERE s.session_key LIKE ?"
                params.append(f"{channel_filter}:%")
            sql += " ORDER BY s.updated_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            conn.close()

            sessions = []
            for r in rows:
                # 解析 session_key 提取通道名
                key = r["session_key"] or ""
                channel = "unknown"
                channel_icon = "❓"
                if ":" in key:
                    ch = key.split(":")[0]
                    channel = ch
                    channel_icon = {"feishu": "🕊️", "telegram": "📡",
                                    "dingtalk": "🔷", "wecom": "💚",
                                    "api": "🌐", "oai_u": "🤖"}.get(ch, "🔌")

                sessions.append({
                    "session_key": key,
                    "channel": channel,
                    "channel_icon": channel_icon,
                    "status": r["status"] or "chatting",
                    "tool_calls": r["tool_calls"] or 0,
                    "token_usage": r["token_usage"] or 0,
                    "model": r["model"] or "—",
                    "goal": (r["goal"] or "")[:60],
                    "updated_at": r["updated_at"] or 0,
                })

            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"sessions": sessions, "total": total},
                                        ensure_ascii=False).encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_auth(self):
        """登录验证：读取 htpasswd 文件校验用户名/密码。用于 Dashboard 表单登录。"""
        import hashlib, os
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: Empty body")
            return
        try:
            body = json.loads(self.rfile.read(content_length).decode('utf-8'))
        except json.JSONDecodeError:
            self.send_error(400, "Bad Request: Invalid JSON")
            return

        username = (body.get('username') or '').strip()
        password = (body.get('password') or '')

        if not username or not password:
            self._send_auth_fail('账号和密码不能为空')
            return

        # 读取 htpasswd 文件
        htpasswd_path = '/etc/nginx/conf.d/dashboard.htpasswd'
        if not os.path.exists(htpasswd_path):
            self._send_auth_fail('服务端配置错误')
            return

        try:
            with open(htpasswd_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if ':' not in line:
                        continue
                    u, pwd_hash = line.split(':', 1)
                    if u != username:
                        continue

                    if self._verify_htpasswd(password, pwd_hash):
                        self.send_response(200)
                        self._send_cors_headers()
                        self.send_header('Content-Type', 'application/json; charset=utf-8')
                        self.end_headers()
                        self.wfile.write(json.dumps({'success': True}, ensure_ascii=False).encode('utf-8'))
                        return
                    else:
                        self._send_auth_fail('账号或密码错误')
                        return
        except Exception:
            pass

        self._send_auth_fail('账号或密码错误')

    def _send_auth_fail(self, msg: str):
        self.send_response(401)
        self._send_cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'success': False, 'error': msg}, ensure_ascii=False).encode('utf-8'))

    @staticmethod
    def _verify_htpasswd(password: str, stored_hash: str) -> bool:
        """验证 Apache htpasswd $apr1$ 格式密码。"""
        import hashlib
        if stored_hash.startswith('$apr1$'):
            # $apr1$<salt>$<hash>
            parts = stored_hash.split('$')
            if len(parts) < 4:
                return False
            salt = parts[2]
            return stored_hash == ApiHandler._apr1_hash(password, salt)
        # 也支持 {SHA} 和明文
        if stored_hash.startswith('{SHA}'):
            import base64
            return stored_hash == '{SHA}' + base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
        # 明文（不推荐，但兼容）
        return password == stored_hash

    @staticmethod
    def _apr1_hash(password: str, salt: str) -> str:
        """Apache $apr1$ MD5 哈希实现。"""
        import hashlib
        def _apr1_md5(pw, slt):
            ctx = hashlib.md5((pw + '$apr1$' + slt).encode('utf-8')).digest()
            ctx = hashlib.md5((pw + slt + pw).encode('utf-8')).digest()
            # 迭代 1000 次
            final = pw + '$apr1$' + slt
            for i in range(1000):
                digest = hashlib.md5()
                if i & 1:
                    digest.update(pw.encode('utf-8'))
                else:
                    digest.update(ctx)
                if i % 3:
                    digest.update(slt.encode('utf-8'))
                if i % 7:
                    digest.update(pw.encode('utf-8'))
                if i & 1:
                    digest.update(ctx)
                else:
                    digest.update(pw.encode('utf-8'))
                ctx = digest.digest()
            # 转 base64
            b64 = './0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
            result = ''
            for a, b, c in ((ctx[i], ctx[i+1] if i+1 < 16 else 0, ctx[i+2] if i+2 < 16 else 0)
                            for i in range(0, 16, 3)):
                result += b64[a & 0x3f]
                result += b64[((a >> 6) & 0x03) | ((b << 2) & 0x3c)]
                result += b64[((b >> 4) & 0x0f)]
                if i + 1 < 16:
                    result += b64[((b >> 2) & 0x03) | ((c << 4) & 0x30)]
                if i + 2 < 16:
                    result += b64[(c >> 2) & 0x3f]
            return '$apr1$' + slt + '$' + result
        return _apr1_md5(password, salt)

    def _handle_edge_report(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: Empty body")
            return
            
        body = self.rfile.read(content_length)
        try:
            req_data = json.loads(body.decode('utf-8'))
        except json.JSONDecodeError:
            self.send_error(400, "Bad Request: Invalid JSON")
            return
            
        node_id = req_data.get('node_id')
        if not node_id:
            self.send_error(400, "Bad Request: Missing node_id")
            return
            
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        report_dir = os.path.join(project_root, 'data', 'sentinel', 'edge_reports')
        os.makedirs(report_dir, exist_ok=True)
        
        # 安全过滤 node_id 防止目录穿越
        import re
        safe_node_id = re.sub(r'[^a-zA-Z0-9_-]', '', node_id)
        if not safe_node_id:
            self.send_error(400, "Bad Request: Invalid node_id")
            return
            
        file_path = os.path.join(report_dir, f"{safe_node_id}.json")
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(req_data, f, ensure_ascii=False, indent=2)

            # ── 1. 先 ACK 边缘 (解锁, 不等 IM) ──
            body_bytes = json.dumps({"status": "success", "message": "Report saved"}).encode('utf-8')
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            self.wfile.flush()                       # ← 保证边缘立刻收到

            # ── 2. 再评估告警 (失败不影响 ACK) ──
            try:
                agent = self.server.api_server.agent
                self._evaluate_edge_alert(agent, safe_node_id, req_data)
            except Exception as e:
                print(f"  ⚠️ [edge_report] 告警评估异常: {e}")
        except Exception as e:
            self.send_error(500, f"Internal Server Error: {str(e)}")

    def _evaluate_edge_alert(self, agent, node: str, data: dict):
        """根据报告内容评估并推送告警。

        三档:
          A. 安全事件 — 按 report_reason (边缘已 diff, 中心不重算)
          B. 绝对资源阈值 — 每次都查 (含心跳/首次, 补 sub-threshold 漂移盲区)
          C. (失联由 cron skill 处理, 见 2.4)
        """
        from core.config_loader import load_config
        from core.alerts import push_alert
        import time

        cfg = load_config() or {}
        acfg = cfg.get('edge', {}).get('alerts', {}) or {}
        if not acfg.get('enabled', True):
            return

        dedup_window = int(acfg.get('dedup_window_min', 10)) * 60
        win = int(time.time() // dedup_window)  # 时间窗进 key

        reason = data.get('report_reason') or ''
        metrics = data.get('metrics', {}) or {}
        security = data.get('security', {}) or {}

        # ── A. 安全事件 (非心跳/非首次 即视为变化) ──
        is_routine = (reason == '首次上报') or reason.startswith('心跳') or ('变化(' in reason) or (reason == '常规登录')
        if reason and not is_routine:
            text = f"🚨 [{node}] 边缘安全事件\n原因: {reason}"
            # 把关键安全字段带上
            af = security.get('auth_fails', 0)
            if af and af > 0: text += f"\nauth_fails(近1h): {af}"
            logins = security.get('recent_logins') or []
            if logins:
                last = logins[-1]
                text += f"\n最近登录: {last.get('user','?')} @ {last.get('ip','?')} ({last.get('method','?')})"
            push_alert(agent, text, title='🚨 Edge 安全告警', color='red',
                       dedup_key=f"{node}:security:{win}:{reason[:30]}")

        # ── B. 绝对资源阈值 (每次都查, 含 routine) ──
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

    def _read_json(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: Empty body")
            return None
        body = self.rfile.read(content_length)
        try:
            return json.loads(body.decode('utf-8'))
        except json.JSONDecodeError:
            self.send_error(400, "Bad Request: Invalid JSON")
            return None

    def _json(self, code, obj):
        self.send_response(code)
        self._send_cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def _handle_pull_task(self, query: str):
        """边缘节点拉取下发任务: GET /api/pull_task?node=<node_id>。

        拉取即 dispatched (原子 claim), 返回验签执行所需 payload 或 {task: null}。"""
        from core import edge_db
        qs = parse_qs(query)
        node = (qs.get('node', [None])[0] or '').strip()
        if not node:
            self.send_error(400, "Bad Request: Missing node")
            return
        task = edge_db.claim_task(node)
        if not task:
            self._quiet = True  # 空轮询不打 access log
            self._json(200, {"task": None})
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
        self._json(200, {"task": payload})

    def _handle_task_result(self):
        """边缘回传执行结果: POST /api/task_result {task_id, exit_code, stdout, stderr}。"""
        from core import edge_db
        body = self._read_json()
        if body is None:
            return
        task_id = body.get('task_id')
        if not task_id:
            self.send_error(400, "Bad Request: Missing task_id")
            return
        try:
            exit_code = int(body.get('exit_code', -1))
        except (TypeError, ValueError):
            exit_code = -1
        updated = edge_db.submit_result(
            task_id, exit_code, body.get('stdout', ''), body.get('stderr', '')
        )
        self._json(200, {"status": "ok" if updated else "noop"})

    def _handle_edge_task(self):
        """管理员上传根私钥签名的高危任务: POST /api/edge_task (admin auth only)。

        cmd 写入后不可变 (id 冲突报 409)。仅接受 key_tier=root。"""
        import uuid
        from core import edge_db
        if getattr(self, 'is_edge', False) or getattr(self, 'is_guest', False):
            self.send_error(403, "Forbidden: admin only")
            return
        body = self._read_json()
        if body is None:
            return
        node, cmd, ts, nonce, sig = (body.get(k) for k in ('node', 'cmd', 'ts', 'nonce', 'sig'))
        if not all([node, cmd, ts, nonce, sig]):
            self.send_error(400, "Bad Request: Missing required fields (node,cmd,ts,nonce,sig)")
            return
        if body.get('key_tier', 'root') != 'root':
            self.send_error(400, "Bad Request: /api/edge_task only accepts key_tier=root")
            return
        task_id = body.get('task_id') or uuid.uuid4().hex
        try:
            edge_db.create_task(task_id, node, cmd, ts, nonce, sig, 'root')
        except Exception as e:
            self.send_error(409, f"Conflict: {e}")
            return
        self._json(200, {"status": "ok", "task_id": task_id})

    def _handle_task_stream(self, query: str):
        qs = parse_qs(query)
        task_id = qs.get('task_id', [None])[0]
        session_id = qs.get('session_id', [None])[0]
        
        if not task_id or not session_id:
            self.send_error(400, "Bad Request: Missing task_id or session_id")
            return

        self.send_response(200)
        self._send_cors_headers()
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        session_mgr = self.server.api_server.agent.session_mgr
        session_key = f"api:{session_id}"

        import time
        max_retries = 300 # 5 minutes max polling
        
        for _ in range(max_retries):
            # Check if client disconnected
            # In python http.server, there's no native non-blocking check, but write will fail if broken pipe
            try:
                progress = session_mgr.load_subtask_dag(session_key, task_id)
                if progress:
                    dag_json, status = progress
                    try:
                        dag_data = json.loads(dag_json)
                    except:
                        dag_data = {}
                        
                    data_obj = {
                        "status": status,
                        "progress": dag_data
                    }
                    self.wfile.write(f"data: {json.dumps(data_obj, ensure_ascii=False)}\n\n".encode('utf-8'))
                    self.wfile.flush()
                    
                    if status in ('done', 'completed', 'failed', 'error'):
                        break
                else:
                    data_obj = {"status": "planning", "message": "正在规划任务..."}
                    self.wfile.write(f"data: {json.dumps(data_obj, ensure_ascii=False)}\n\n".encode('utf-8'))
                    self.wfile.flush()
            except Exception as e:
                # Client probably disconnected
                break
                
            time.sleep(1)

    def _handle_email_html(self, query: str):
        """直接返回邮件原始 HTML，供浏览器原生渲染预览。
        GET /api/v1/email/html?account=<account>&uid=<uid>
        """
        import os, sqlite3
        qs = parse_qs(query)
        account = (qs.get('account', [None])[0] or '').strip()
        uid = (qs.get('uid', [None])[0] or '').strip()
        if not account or not uid:
            self.send_error(400, "Bad Request: Missing account or uid")
            return

        from core.config_loader import load_config
        cfg = load_config() or {}
        billing_dir = cfg.get('billing', {}).get('script_dir', '/home/liteagent/mail-statement-parser')
        db_path = os.path.join(billing_dir, 'statements.db')
        if not os.path.exists(db_path):
            self.send_error(500, "Database not found")
            return

        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT es.subject, es.sender, es.email_date, eb.raw_html, eb.plain_text "
                "FROM email_bodies eb "
                "JOIN email_summaries es ON eb.account_name=es.account_name AND eb.uid=es.uid "
                "WHERE eb.account_name=? AND eb.uid=?", (account, uid)
            )
            row = cur.fetchone()
            conn.close()
        except Exception as e:
            self.send_error(500, f"Database error: {e}")
            return

        if not row:
            self.send_error(404, "Email not found")
            return

        subject, sender, email_date, raw_html, plain_text = row
        if raw_html:
            html_content = raw_html
        elif plain_text:
            # 纯文本用 <pre> 包裹
            import html
            html_content = f"<html><head><meta charset='utf-8'><title>{html.escape(subject or '')}</title></head><body><pre>{html.escape(plain_text)}</pre></body></html>"
        else:
            self.send_error(404, "Email body is empty")
            return

        body_bytes = html_content.encode('utf-8')
        self.send_response(200)
        self._send_cors_headers()
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _handle_todos(self, query: str):
        qs = parse_qs(query)
        status = qs.get("status", ["pending,active"])[0]
        try:
            from skills.ops_todo import get_todos_json
            todos = get_todos_json(status=status)
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "data": todos}).encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_openai_models(self):
        models_obj = {
            "object": "list",
            "data": [
                {
                    "id": "lite-agent",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "lite-agent"
                }
            ]
        }
        self.send_response(200)
        self._send_cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(models_obj).encode('utf-8'))

    def _handle_openai_chat_completions(self):
        import uuid
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: Empty body")
            return
            
        body = self.rfile.read(content_length)
        try:
            req_data = json.loads(body.decode('utf-8'))
        except json.JSONDecodeError:
            self.send_error(400, "Bad Request: Invalid JSON")
            return

        messages = req_data.get('messages', [])
        if not messages:
            self.send_error(400, "Bad Request: Missing messages")
            return
            
        text = ""
        for m in reversed(messages):
            if m.get('role') == 'user':
                text = m.get('content', '')
                break
                
        if not text:
            self.send_error(400, "Bad Request: No user message found")
            return
            
        client_user = req_data.get('user', '')
        is_guest_mode = getattr(self, "is_guest", False)
        
        if client_user:
            session_id = f"oai_u_{client_user}"
        else:
            role_name = "guest" if is_guest_mode else "admin"
            session_id = f"oai_{role_name}"
            
        msg = IncomingMessage(
            channel='api',
            user_id=session_id,
            chat_id=session_id,
            message_id=str(time.time()),
            text=text,
            notify_channels=[],
            is_guest=is_guest_mode,
            sync_mode=True
        )
        
        agent = self.server.api_server.agent
        resp = agent.handle(msg)
        
        final_text = ""
        if not resp:
            final_text = ""
        else:
            final_text = resp.text

        is_stream = req_data.get('stream', False)
        
        if is_stream:
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            
            chunk_obj = {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": req_data.get("model", "lite-agent"),
                "choices": [{"index": 0, "delta": {"content": final_text}}]
            }
            self.wfile.write(f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n".encode('utf-8'))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            resp_obj = {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req_data.get("model", "lite-agent"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": final_text
                        },
                        "finish_reason": "stop"
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                }
            }
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(resp_obj, ensure_ascii=False).encode('utf-8'))


class ApiServer:
    """独立的 API 服务端，专门处理 Web 界面和第三方系统的 REST/SSE 请求"""

    def __init__(self, config: dict, agent):
        self.config = config.get("api", {})
        self.agent = agent
        self.host = self.config.get("host", "0.0.0.0")
        self.port = self.config.get("port", 8080)
        self.auth_token = self.config.get("auth_token", "")
        self.server = None
        self._thread = None

    def start(self):
        if not self.config.get("enabled", False):
            print("  ⚠️ API 通道未启用")
            return

        self.server = ThreadingHTTPServer((self.host, self.port), ApiHandler)
        self.server.api_server = self  # 给 Handler 注入引用
        
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="ApiServer")
        self._thread.start()
        print(f"  📡 API Server 启动成功 (http://{self.host}:{self.port})")

    def stop(self):
        if self.server:
            # shutdown must be called from a different thread to avoid deadlock
            threading.Thread(target=self.server.shutdown).start()
            print("  🛑 API Server 已停止")

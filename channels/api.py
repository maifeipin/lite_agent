import json
import re
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
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PATCH, DELETE, OPTIONS')
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
        req_path = parsed_url.path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'

        # 仪表盘 API 无需认证（仅返回注册表指令列表，无敏感数据）
        if req_path == '/api/v1/dashboard':
            self._handle_dashboard()
            return

        if not self._auth():
            return
        if self.is_guest and (
            req_path.startswith('/api/v1/task-specs')
            or req_path.startswith('/api/v1/output-archive/')
        ):
            self.send_error(403, "Forbidden: admin access required")
            return
        
        # 边缘节点权限隔离：仅允许 /api/report, /api/pull_task
        if getattr(self, 'is_edge', False) and req_path not in ('/api/report', '/api/pull_task'):
            self.send_error(403, "Forbidden: Edge token is limited to /api/report, /api/pull_task")
            return

        if req_path == '/api/pull_task':
            self._handle_pull_task(parsed_url.query)
        elif req_path == '/api/v1/sessions':
            self._handle_sessions(parsed_url.query)
        elif req_path == '/api/v1/sessions/messages':
            self._handle_session_messages(parsed_url.query)
        elif req_path == '/api/v1/task/stream':
            self._handle_task_stream(parsed_url.query)
        elif req_path == '/api/v1/email/html':
            self._handle_email_html(parsed_url.query)
        elif req_path == '/api/v1/todos':
            self._handle_todos(parsed_url.query)
        elif req_path == '/api/v1/task-specs':
            self._handle_task_specs_get(parsed_url.query)
        elif req_path == '/api/v1/task-specs/meta':
            self._handle_task_specs_meta()
        elif req_path.startswith('/api/v1/task-specs/'):
            self._handle_task_spec_get(req_path)
        elif req_path.startswith('/api/v1/output-archive/'):
            self._handle_output_archive_get(req_path)
        elif req_path == '/api/v1/socks5':
            self._handle_socks5_get(parsed_url.query)
        elif req_path == '/api/v1/socks5/active':
            self._handle_socks5_active_get()
        elif req_path == '/api/v1/socks5/health':
            self._handle_socks5_health(parsed_url.query)
        elif req_path == '/api/v1/socks5/test':
            self._handle_socks5_test(parsed_url.query)
        elif req_path == '/api/v1/socks5/script':
            self._handle_socks5_script(parsed_url.query)
        elif req_path == '/api/v1/dashboard':
            self._handle_dashboard()
        elif req_path == '/v1/models':
            self._handle_openai_models()
        elif req_path == '/api/v1/rss/brief':
            self._handle_rss_brief()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed_url = urlparse(self.path)
        req_path = parsed_url.path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'

        # 登录接口无需认证
        if req_path == '/api/v1/auth':
            self._handle_auth()
            return

        if not self._auth():
            return
        if self.is_guest and req_path.startswith('/api/v1/task-specs'):
            self.send_error(403, "Forbidden: TaskSpec management requires admin")
            return
        
        # 边缘节点权限隔离：仅允许 /api/report, /api/task_result
        if getattr(self, 'is_edge', False) and req_path not in ('/api/report', '/api/task_result'):
            self.send_error(403, "Forbidden: Edge token is limited to /api/report, /api/task_result")
            return

        if req_path in ('/api/v1/chat', '/api/v1/task'):
            self._handle_chat_or_task()
        elif req_path == '/api/v1/alert':
            self._handle_alert()
        elif req_path == '/v1/chat/completions':
            self._handle_openai_chat_completions()
        elif req_path == '/api/v1/dashboard':
            self._handle_dashboard()
        elif req_path == '/api/report':
            self._handle_edge_report()
        elif req_path == '/api/task_result':
            self._handle_task_result()
        elif req_path == '/api/edge_task':
            self._handle_edge_task()
        elif req_path == '/api/v1/ocr':
            self._handle_ocr_proxy()
        elif req_path == '/api/v1/todos':
            self._handle_post_todo()
        elif req_path == '/api/v1/task-specs':
            self._handle_task_spec_create()
        elif req_path == '/api/v1/task-specs/generate':
            self._handle_task_spec_generate()
        elif req_path.startswith('/api/v1/task-specs/'):
            self._handle_task_spec_action(req_path)
        elif req_path == '/api/v1/todos/brief/push':
            self._handle_post_todo_brief_push()
        elif req_path == '/api/v1/session/title':
            self._handle_post_session_title()
        elif req_path == '/api/v1/socks5':
            self._handle_socks5_post()
        elif req_path == '/api/v1/socks5/active':
            self._handle_socks5_active_post()
        else:
            self.send_error(404, "Not Found")

    def do_PATCH(self):
        parsed_url = urlparse(self.path)
        if getattr(self, 'is_edge', False):
            self.send_error(403, "Forbidden")
            return
        if not self._auth():
            return
        req_path = parsed_url.path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'
        if self.is_guest and req_path.startswith('/api/v1/task-specs'):
            self.send_error(403, "Forbidden: TaskSpec management requires admin")
            return
        if req_path.startswith('/api/v1/todos/'):
            self._handle_patch_todo(req_path)
        elif req_path.startswith('/api/v1/task-specs/'):
            self._handle_task_spec_update(req_path)
        elif req_path.startswith('/api/v1/socks5/'):
            self._handle_socks5_patch(req_path)
        else:
            self.send_error(404, "Not Found")

    def do_DELETE(self):
        parsed_url = urlparse(self.path)
        if getattr(self, 'is_edge', False):
            self.send_error(403, "Forbidden")
            return
        if not self._auth():
            return
        req_path = parsed_url.path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'
        if self.is_guest and req_path.startswith('/api/v1/task-specs'):
            self.send_error(403, "Forbidden: TaskSpec management requires admin")
            return
        if req_path.startswith('/api/v1/todos/'):
            self._handle_delete_todo(req_path)
        elif req_path.startswith('/api/v1/task-specs/'):
            self._handle_task_spec_delete(req_path)
        elif req_path.startswith('/api/v1/socks5/'):
            self._handle_socks5_delete(req_path)
        else:
            self.send_error(404, "Not Found")

    def _handle_ocr_proxy(self):
        import os
        import requests
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: Empty body")
            return
            
        body = self.rfile.read(content_length)
        ocr_url = os.environ.get('OCR_ENDPOINT', 'http://127.0.0.1:8000/api/ocr')
        
        headers = {
            'Content-Type': self.headers.get('Content-Type')
        }
        
        try:
            res = requests.post(ocr_url, data=body, headers=headers, timeout=30)
            self.send_response(res.status_code)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(res.content)
        except Exception as e:
            self.send_response(500)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"detail": f"OCR Proxy error: {str(e)}"}).encode('utf-8'))

    def _handle_rss_brief(self):
        import os
        
        # Load vps_work_dir dynamically from paths.json if exists to prevent hardcoding
        vps_work_dir = '/home/liteagent/rss_topic_work'
        paths_json_path = '/home/liteagent/rss_topic_work/paths.json'
        
        env_vps_work = os.environ.get("RSS_VPS_WORK_DIR")
        if env_vps_work:
            vps_work_dir = os.path.expanduser(env_vps_work)
        elif os.path.exists(paths_json_path):
            try:
                with open(paths_json_path, 'r', encoding='utf-8') as f:
                    pcfg = json.load(f)
                    vps_work_dir = os.path.expanduser(pcfg.get("vps_work_dir", vps_work_dir))
            except Exception:
                pass
                
        brief_path = os.path.join(vps_work_dir, 'latest_brief.json')
        if not os.path.exists(brief_path):
            self.send_response(404)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"detail": "Brief not found"}).encode('utf-8'))
            return
            
        try:
            with open(brief_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(content.encode('utf-8'))
        except Exception as e:
            self.send_response(500)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"detail": str(e)}).encode('utf-8'))

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
            notify_channels=notify_channels,
            output_mode=str(req_data.get('output_delivery') or ''),
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

        session_obj = agent.session_mgr.get_or_create(msg.session_key)

        if resp.task_id:
            # 这是一个异步长任务
            out_data = {
                "type": "async",
                "task_id": resp.task_id,
                "message": resp.text,
                "title": session_obj.title
            }
        else:
            # 同步返回
            out_data = {
                "type": "sync",
                "status": "completed",
                "response": resp.text,
                "logs": getattr(resp, "logs", []),
                "title": session_obj.title
            }
        if getattr(resp, "new_session_key", ""):
            out_data["session_key"] = resp.new_session_key
            out_data["new_session_id"] = resp.new_session_key.removeprefix("api:")

        self.wfile.write(json.dumps(out_data, ensure_ascii=False).encode('utf-8'))

    def _handle_alert(self):
        """透传告警到 IM (push_alert), 非阻塞: 先 ACK 200 再推。
        供 standalone 脚本(hotspot.py/topic_diff.py)绕开 agent.handle 用。
        仅 admin (非 guest; edge 已被 do_POST 白名单挡在 /api/report,/api/task_result)。
        Body: {title?, text, color?, dedup_key?}"""
        if getattr(self, 'is_guest', False):
            self.send_error(403, "Forbidden: admin token required")
            return
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: Empty body")
            return
        body = self.rfile.read(content_length)
        try:
            req = json.loads(body.decode('utf-8'))
        except json.JSONDecodeError:
            self.send_error(400, "Bad Request: Invalid JSON")
            return
        text = (req.get('text') or '').strip()
        if not text:
            self.send_error(400, "Bad Request: Missing text")
            return
        title = req.get('title') or '📢 告警'
        color = req.get('color') or 'red'
        dedup_key = req.get('dedup_key')

        # 1. 先 ACK 200 (调用方不阻塞/不超时; 镜像 _handle_edge_report 的 ACK-first)
        ack = json.dumps({"status": "accepted"}).encode('utf-8')
        self.send_response(200)
        self._send_cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(ack)))
        self.end_headers()
        self.wfile.write(ack)
        self.wfile.flush()

        # 2. 再推 (push_alert 永不 raise, 失败只 print)
        try:
            from core.alerts import push_alert
            agent = self.server.api_server.agent
            push_alert(agent, text, title=title, color=color, dedup_key=dedup_key)
        except Exception as e:
            print(f"  ⚠️ [alert] push_alert 异常: {e}")

    def _handle_dashboard(self):
        """返回仪表盘可用的指令列表（来自注册表）。"""
        from core.command_registry import CommandRegistry
        items = CommandRegistry().items_for_dashboard()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(items, ensure_ascii=False).encode('utf-8'))

    def _read_json_body(self) -> dict:
        length = int(self.headers.get('Content-Length', 0))
        if length <= 0:
            raise ValueError("Empty JSON body")
        value = json.loads(self.rfile.read(length).decode('utf-8'))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _send_json(self, value, status: int = 200):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(status)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionResetError):
            # The work may have completed after a browser refresh. Do not turn a
            # successful persisted operation into a second, impossible response.
            self._quiet = True
            return False

    @property
    def _task_specs(self):
        return self.server.api_server.task_specs

    def _handle_task_specs_get(self, query: str = ""):
        items = self._task_specs.store.list()
        self._send_json({"data": items, "total": len(items)})

    def _handle_task_specs_meta(self):
        from core.task_spec import BASE_POLICY, SCHEMA_VERSION, policy_digest
        models = []
        for name, cfg in self._task_specs.router.models_cfg.items():
            models.append({
                "name": name,
                "model": cfg.get("model", name),
                "tags": cfg.get("tags", []),
            })
        self._send_json({
            "schema_version": SCHEMA_VERSION,
            "policy": BASE_POLICY,
            "policy_digest": policy_digest(),
            "models": models,
            "capabilities": self._task_specs.capability_map,
            "author_model": self._task_specs.author_model,
            "validator_model": self._task_specs.validator_model,
            "model_tiers": (
                (self._task_specs.config.get("task_specs", {}) or {})
                .get("model_tiers", {}) or {}
            ),
            "tools": [
                schema.get("function", {})
                for schema in (
                    self._task_specs.skill_engine.get_all_schemas()
                    if self._task_specs.skill_engine is not None else []
                )
            ],
        })

    def _handle_task_spec_get(self, path: str):
        task_id = path.rstrip('/').split('/')[-1]
        item = self._task_specs.store.get(task_id)
        if item is None:
            self._send_json({"error": "TaskSpec not found"}, 404)
            return
        self._send_json(item)

    def _handle_output_archive_get(self, path: str):
        archive_id = path.rstrip('/').split('/')[-1]
        if not re.fullmatch(r"[0-9a-f]{16}", archive_id):
            self._send_json({"error": "Invalid archive id"}, 400)
            return
        from core.output_delivery import get_archived_output
        item = get_archived_output(archive_id, self.server.api_server.agent._config)
        if item is None:
            self._send_json({"error": "Output archive not found"}, 404)
            return
        self._send_json(item)

    def _handle_task_spec_create(self):
        try:
            data = self._read_json_body()
            if isinstance(data.get("spec"), dict):
                created = self._task_specs.import_spec(data["spec"])
            else:
                goal = str(data.get("goal") or "").strip()
                if not goal:
                    raise ValueError("goal is required")
                created = self._task_specs.create_manual(goal, str(data.get("name") or ""))
            self._send_json(created, 201)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, 400)

    def _handle_task_spec_generate(self):
        """Compatibility endpoint: persist first; AI enrichment is explicit."""
        try:
            data = self._read_json_body()
            goal = str(data.get("goal") or "").strip()
            if not goal:
                raise ValueError("goal is required")
            result = self._task_specs.create_manual(
                goal, str(data.get("name") or "")
            )
            result["generation"] = {
                "status": "not_started",
                "message": "基础规则已创建；AI 完善为可选步骤",
            }
            self._send_json(result, 201)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"error": f"TaskSpec creation failed: {exc}"}, 500)

    def _handle_task_spec_update(self, path: str):
        task_id = path.rstrip('/').split('/')[-1]
        try:
            data = self._read_json_body()
            spec = data.get("spec") if isinstance(data.get("spec"), dict) else data
            result = self._task_specs.update(task_id, spec)
            self._send_json(result)
        except KeyError:
            self._send_json({"error": "TaskSpec not found"}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, 400)

    def _handle_task_spec_action(self, path: str):
        from core.task_spec_service import TaskSpecRevisionConflict

        parts = path.rstrip('/').split('/')
        if len(parts) < 6:
            self._send_json({"error": "Missing TaskSpec action"}, 400)
            return
        task_id, action = parts[-2], parts[-1]
        try:
            if action == "enrich":
                data = self._read_json_body() if int(
                    self.headers.get('Content-Length', 0)
                ) > 0 else {}
                result = self._task_specs.enrich(
                    task_id, str(data.get("model") or "")
                )
            elif action == "validate":
                result = self._task_specs.review(task_id)
            elif action == "confirm":
                result = self._task_specs.confirm_generated(task_id)
            elif action == "acknowledge":
                data = self._read_json_body()
                result = self._task_specs.acknowledge(task_id, str(data.get("rationale") or ""))
            elif action == "schedule":
                data = self._read_json_body()
                current = self._task_specs.store.get(task_id)
                if current is None:
                    raise KeyError(task_id)
                if current["status"] != "approved":
                    raise ValueError("只有已通过校验的任务才能启用调度")
                enabled = bool(data.get("enabled", True))
                schedule_mode = (
                    ((current["spec"].get("execution") or {}).get("schedule") or {})
                    .get("mode", "manual")
                )
                if enabled and schedule_mode == "manual":
                    raise ValueError("手动任务没有调度时间，请先设置一次或重复计划")
                result = self._task_specs.store.save(
                    current["spec"], status="approved", enabled=enabled
                )
            elif action == "run":
                result = self.server.api_server.start_task_spec_run(task_id)
            else:
                self._send_json({"error": f"Unknown action: {action}"}, 404)
                return
            self._send_json(result, 202 if action == "run" else 200)
        except KeyError:
            self._send_json({"error": "TaskSpec not found"}, 404)
        except TaskSpecRevisionConflict as exc:
            self._send_json({"error": str(exc), "code": "REVISION_CONFLICT"}, 409)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_task_spec_delete(self, path: str):
        task_id = path.rstrip('/').split('/')[-1]
        if self._task_specs.store.delete(task_id):
            self._send_json({"success": True})
        else:
            self._send_json({"error": "TaskSpec not found"}, 404)

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
                       s.token_usage, s.updated_at, s.goal, s.title,
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
                    "title": r["title"] or "",
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

    def _handle_post_session_title(self):
        """手动修改会话标题: POST /agent/api/v1/session/title"""
        body = self._read_json()
        if not body:
            return
        session_key = body.get("session_key")
        title = (body.get("title") or "").strip()
        if not session_key:
            self.send_error(400, "Missing session_key")
            return
        session_mgr = self.server.api_server.agent.session_mgr
        if session_key not in session_mgr._cache:
            import sqlite3
            with sqlite3.connect(session_mgr.db_path) as conn:
                row = conn.execute("SELECT 1 FROM sessions WHERE session_key=?", (session_key,)).fetchone()
                if not row:
                    self.send_error(404, f"Session {session_key} not found")
                    return
        session_mgr.set_title(session_key, title)
        self.send_response(200)
        self._send_cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "session_key": session_key, "title": title}, ensure_ascii=False).encode('utf-8'))

    def _handle_session_messages(self, query: str):
        """返回指定会话的最近消息列表。"""
        import sqlite3, os
        qs = parse_qs(query)
        session_key = qs.get("session_key", [None])[0]
        limit = int(qs.get("limit", ["20"])[0])

        if not session_key:
            self.send_error(400, "Missing session_key")
            return

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "data", "sessions.db")

        if not os.path.exists(db_path):
            self._send_json({"messages": []})
            return

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT role, content, reasoning_content, tool_call_id, name, tool_calls_json, created_at FROM messages "
                "WHERE session_key = ? ORDER BY created_at DESC LIMIT ?",
                (session_key, limit)
            ).fetchall()
            conn.close()

            messages = []
            for r in reversed(rows):
                content = (r["content"] or "")
                msg_item = {
                    "role": r["role"],
                    "content": content,
                    "time": r["created_at"] or 0,
                }
                if r["reasoning_content"]:
                    msg_item["reasoning_content"] = r["reasoning_content"]
                if r["name"]:
                    msg_item["name"] = r["name"]
                if r["tool_call_id"]:
                    msg_item["tool_call_id"] = r["tool_call_id"]
                if r["tool_calls_json"]:
                    try:
                        msg_item["tool_calls"] = json.loads(r["tool_calls_json"])
                    except Exception:
                        pass
                messages.append(msg_item)

            self._send_json({"messages": messages})
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
        """验证 Apache htpasswd 格式密码。支持 bcrypt $2b$ / $2y$ / $apr1$ / {SHA} / 明文。"""
        import hashlib
        # bcrypt ($2b$ or $2y$)
        if stored_hash.startswith('$2b$') or stored_hash.startswith('$2y$'):
            try:
                import bcrypt
                return bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8'))
            except ImportError:
                return False
        # apr1
        if stored_hash.startswith('$apr1$'):
            parts = stored_hash.split('$')
            if len(parts) < 4:
                return False
            salt = parts[2]
            return stored_hash == ApiHandler._apr1_hash(password, salt)
        # SHA
        if stored_hash.startswith('{SHA}'):
            import base64
            return stored_hash == '{SHA}' + base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
        # 明文
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
        last_sent_log_idx = 0
        
        for _ in range(max_retries):
            try:
                progress = session_mgr.load_subtask_dag(session_key, task_id)
                if progress:
                    dag_json, status = progress
                    try:
                        dag_data = json.loads(dag_json)
                    except:
                        dag_data = {}
                        
                    all_logs = dag_data.get("logs", []) if isinstance(dag_data, dict) else []
                    new_logs = all_logs[last_sent_log_idx:]
                    last_sent_log_idx = len(all_logs)

                    final_resp = dag_data.get("final_result", "") if isinstance(dag_data, dict) else ""
                    data_obj = {
                        "status": status,
                        "response": final_resp,
                        "progress": dag_data,
                        "logs": new_logs,
                        "total_logs": len(all_logs)
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

    def _handle_post_todo(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: Empty body")
            return
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode('utf-8'))
            title = data.get("title")
            if not title:
                self.send_error(400, "Bad Request: Missing title")
                return
            kind = data.get("kind", "misc")
            project = data.get("project")
            description = data.get("description")
            due_at = data.get("due_at")
            remind_interval_mins = data.get("remind_interval_mins", 0)
            remind_before_mins = data.get("remind_before_mins", 30)

            from skills.ops_todo import todo_add
            msg = todo_add(title=title, kind=kind, project=project, description=description, due_at=due_at, remind_interval_mins=remind_interval_mins, remind_before_mins=remind_before_mins)
            
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": msg}).encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_post_todo_brief_push(self):
        try:
            from skills.ops_todo import todo_push_brief
            msg = todo_push_brief()
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": msg}).encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_patch_todo(self, path: str):
        tid = path.split('/')[-1]
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: Empty body")
            return
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode('utf-8'))
            msg = []

            from skills.ops_todo import todo_done, todo_start, todo_resume, todo_update

            status = data.get("status")
            if status:
                if status == 'done':
                    msg.append(todo_done(tid))
                elif status == 'active':
                    msg.append(todo_start(tid))
                elif status == 'pending':
                    msg.append(todo_resume(tid))
                else:
                    self.send_error(400, f"Unsupported status: {status}")
                    return

            title = data.get("title")
            description = data.get("description")
            due_at = data.get("due_at")
            project = data.get("project")
            recur_cron = data.get("recur_cron")
            kind = data.get("kind")
            remind_interval_mins = data.get("remind_interval_mins")
            remind_before_mins = data.get("remind_before_mins")

            if any(x is not None for x in [title, description, due_at, project, recur_cron, kind, remind_interval_mins, remind_before_mins]):
                msg.append(todo_update(tid, title=title, description=description, due_at=due_at, project=project, recur_cron=recur_cron, kind=kind, remind_interval_mins=remind_interval_mins, remind_before_mins=remind_before_mins))

            if not msg:
                self.send_error(400, "Bad Request: No fields to update")
                return

            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": " | ".join(msg)}).encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_delete_todo(self, path: str):
        tid = path.split('/')[-1]
        try:
            from skills.ops_todo import _conn, _render_markdown
            import sqlite3
            conn = _conn()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM todos WHERE id=?", (tid,))
            rowcount = cursor.rowcount
            conn.commit()
            conn.close()

            if rowcount > 0:
                _render_markdown()
                msg = f"✅ 任务 {tid} 已被永久删除"
            else:
                self.send_response(404)
                self._send_cors_headers()
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "detail": f"Todo {tid} not found"}).encode('utf-8'))
                return

            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": msg}).encode('utf-8'))
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
            sync_mode=True,
            output_mode=str(req_data.get('output_delivery') or ''),
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
            if resp and getattr(resp, "new_session_key", ""):
                resp_obj["session_key"] = resp.new_session_key
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(resp_obj, ensure_ascii=False).encode('utf-8'))

    # ── Socks5 Proxy Management Handlers ──
    def _handle_socks5_get(self, query: str):
        qs = parse_qs(query)
        q = qs.get("q", [None])[0]
        try:
            from skills.ops_socks5 import get_socks5_proxies
            proxies = get_socks5_proxies(query=q)
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "data": proxies}).encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_socks5_post(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: Empty body")
            return
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode('utf-8'))
            servername = data.get("servername")
            host = data.get("host")
            if not servername or not host:
                self.send_error(400, "Bad Request: Missing servername or host")
                return
            runcmd = data.get("runcmd", "")
            clientproxy = data.get("clientproxy", "")
            memo = data.get("memo", "")

            from skills.ops_socks5 import add_socks5_proxy
            new_id = add_socks5_proxy(host=host, runcmd=runcmd, servername=servername, clientproxy=clientproxy, memo=memo)
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "id": new_id}).encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_socks5_patch(self, path: str):
        proxy_id_str = path.rsplit('/', 1)[-1]
        try:
            proxy_id = int(proxy_id_str)
        except ValueError:
            self.send_error(400, "Invalid proxy ID")
            return
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            data = json.loads(body.decode('utf-8'))
            from skills.ops_socks5 import update_socks5_proxy
            ok = update_socks5_proxy(
                proxy_id=proxy_id,
                host=data.get("host"),
                runcmd=data.get("runcmd"),
                servername=data.get("servername"),
                clientproxy=data.get("clientproxy"),
                memo=data.get("memo")
            )
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": ok}).encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_socks5_delete(self, path: str):
        proxy_id_str = path.rsplit('/', 1)[-1]
        try:
            proxy_id = int(proxy_id_str)
        except ValueError:
            self.send_error(400, "Invalid proxy ID")
            return
        try:
            from skills.ops_socks5 import delete_socks5_proxy
            ok = delete_socks5_proxy(proxy_id)
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": ok}).encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_socks5_test(self, query: str):
        qs = parse_qs(query)
        proxy_id_str = qs.get("id", [None])[0]
        host = qs.get("host", [None])[0]
        port_str = qs.get("port", [None])[0]
        port = int(port_str) if port_str and port_str.isdigit() else None

        from skills.ops_socks5 import test_socks5_host, get_socks5_proxy_by_id
        runcmd = ""
        if proxy_id_str and proxy_id_str.isdigit():
            proxy = get_socks5_proxy_by_id(int(proxy_id_str))
            if proxy:
                host = proxy.get("host")
                runcmd = proxy.get("runcmd", "")

        if not host:
            self.send_error(400, "Missing host or valid proxy id")
            return

        res = test_socks5_host(host=host, runcmd=runcmd, port=port)
        self.send_response(200)
        self._send_cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({"success": res["success"], "result": res}).encode('utf-8'))

    def _handle_socks5_script(self, query: str):
        qs = parse_qs(query)
        proxy_id_str = qs.get("id", [None])[0]
        script_type = qs.get("type", ["ps1"])[0].lower()
        if not proxy_id_str or not proxy_id_str.isdigit():
            self.send_error(400, "Missing proxy id")
            return
        
        from skills.ops_socks5 import get_socks5_proxy_by_id, generate_ps1_script, generate_sh_script
        proxy = get_socks5_proxy_by_id(int(proxy_id_str))
        if not proxy:
            self.send_error(404, "Proxy not found")
            return
        
        if script_type == "sh":
            script_content = generate_sh_script(proxy)
        else:
            script_content = generate_ps1_script(proxy)
            
        self.send_response(200)
        self._send_cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({"success": True, "type": script_type, "script": script_content}).encode('utf-8'))

    def _handle_socks5_active_get(self):
        from skills.ops_socks5 import get_current_active_proxy
        active = get_current_active_proxy()
        self.send_response(200)
        self._send_cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({"success": True, "data": active}).encode('utf-8'))

    def _handle_socks5_active_post(self):
        if getattr(self, 'is_edge', False) or getattr(self, 'is_guest', False):
            self.send_error(403, "Forbidden: Only master token can perform active proxy switching")
            return
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: Empty body")
            return
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode('utf-8'))
            proxy_id = data.get("id")
            if not proxy_id:
                self.send_error(400, "Missing proxy id")
                return
            from skills.ops_socks5 import apply_active_proxy_to_vps1
            ok, msg = apply_active_proxy_to_vps1(int(proxy_id))
            self.send_response(200 if ok else 400)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": ok, "message": msg}).encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_socks5_health(self, query: str = ""):
        try:
            from urllib.parse import parse_qs
            qs = parse_qs(query)
            proxy_id_list = qs.get("id") or qs.get("proxy_id")
            
            if proxy_id_list and proxy_id_list[0].isdigit():
                from skills.ops_socks5 import test_socks5_proxy_outbound
                res = test_socks5_proxy_outbound(int(proxy_id_list[0]))
            else:
                from skills.ops_socks5 import test_socks5_outbound_http
                res = test_socks5_outbound_http()
                
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": res.get("success", False), "result": res}).encode('utf-8'))
        except Exception as e:
            self.send_error(500, f"Socks5 Health probe error: {str(e)}")


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
        from core.task_spec_service import TaskSpecService
        self.task_specs = TaskSpecService(
            agent._config, agent.skill_engine, ledger=agent.ledger,
            request_selector=getattr(agent, "request_selector", None),
        )
        self.task_specs.store.recover_interrupted()
        self._task_run_lock = threading.Lock()
        self._running_task_specs = set()

    @staticmethod
    def _render_task_spec_prompt(spec: dict) -> str:
        task = spec.get("task") or {}
        execution = spec.get("execution") or {}
        output = spec.get("output") or {}
        return (
            "请严格执行以下已审批 TaskSpec。不得扩大目标、权限或副作用范围。\n\n"
            f"目标: {task.get('objective', '')}\n"
            f"背景: {task.get('context', '')}\n"
            f"约束: {json.dumps(task.get('constraints', []), ensure_ascii=False)}\n"
            f"验收标准: {json.dumps(task.get('acceptance_criteria', []), ensure_ascii=False)}\n"
            f"网络条件: {json.dumps(execution.get('network', {}), ensure_ascii=False)}\n"
            f"能力: {json.dumps(execution.get('capabilities', []), ensure_ascii=False)}\n"
            f"已审批计划: {json.dumps(execution.get('plan', []), ensure_ascii=False)}\n"
            f"输出要求: {json.dumps(output, ensure_ascii=False)}"
        )

    def start_task_spec_run(self, task_id: str, scheduled: bool = False) -> dict:
        current = self.task_specs.store.get(task_id)
        if current is None:
            raise KeyError(task_id)
        if current["status"] != "approved":
            raise ValueError("TaskSpec 尚未通过校验")
        validated = current["spec"].get("contract", {}).get("validated_digest")
        from core.task_spec import content_digest
        if validated != content_digest(current["spec"]):
            raise ValueError("TaskSpec 内容已变化，需要重新校验")
        with self._task_run_lock:
            if task_id in self._running_task_specs:
                raise ValueError("TaskSpec 正在执行")
            self._running_task_specs.add(task_id)
        self.task_specs.store.mark_started(task_id, scheduled=scheduled)

        def _run():
            try:
                from core.task_orchestrator import TaskOrchestrator
                item = self.task_specs.store.get(task_id)
                spec = item["spec"]
                budget = (spec.get("execution") or {}).get("budget") or {}
                orchestrator = TaskOrchestrator(
                    config=self.agent._config,
                    skill_engine=self.agent.skill_engine,
                    session_mgr=self.agent.session_mgr,
                    channels=self.agent.channels,
                    ledger=self.agent.ledger,
                )
                planned = self.task_specs.build_subtasks(spec)
                execution_policy = self.task_specs.build_execution_policy(spec)
                if planned:
                    # Direct nodes in an approved immutable TaskSpec do not need a
                    # Worker LLM. SkillEngine still enforces the tool allowlist.
                    orchestrator.direct_tool_execution = True
                result = orchestrator.execute(
                    self._render_task_spec_prompt(spec),
                    session_key=f"task_spec:{task_id}",
                    task_id=f"spec_{task_id[:8]}",
                    step_override=int(budget.get("max_steps", 20)),
                    token_override=int(budget.get("max_total_tokens", 50000)),
                    parallel_override=int(budget.get("max_parallel_tasks", 3)),
                    wall_seconds_override=int(budget.get("max_wall_seconds", 900)),
                    planned_subtasks=planned or None,
                    planned_strategy=str((spec.get("task") or {}).get("context") or ""),
                    execution_policy=execution_policy,
                )
                delivered_result = self.agent._prepare_output(
                    result, "task", overrides=spec.get("output") or {},
                    title=str((spec.get("task") or {}).get("name") or "TaskSpec 完整回复"),
                    session_key=f"task_spec:{task_id}",
                )
                self.task_specs.store.mark_finished(task_id, True, delivered_result)
            except Exception as exc:
                self.task_specs.store.mark_finished(task_id, False, str(exc))
            finally:
                with self._task_run_lock:
                    self._running_task_specs.discard(task_id)

        threading.Thread(
            target=_run, daemon=True, name=f"TaskSpec-{task_id[:8]}"
        ).start()
        return {"status": "accepted", "task_id": task_id}

    def run_due_task_specs(self):
        started = 0
        for item in self.task_specs.store.due():
            try:
                self.start_task_spec_run(item["id"], scheduled=True)
                started += 1
            except Exception as exc:
                print(f"  ⚠️ [TaskSpec] 定时任务 {item['id']} 未启动: {exc}")
        return f"TaskSpec scheduler: started={started}"

    def start(self):
        if not self.config.get("enabled", False):
            print("  ⚠️ API 通道未启用")
            return

        self.server = ThreadingHTTPServer((self.host, self.port), ApiHandler)
        self.server.api_server = self  # 给 Handler 注入引用

        if not any(job.name == "task_specs_tick" for job in self.agent.cron.jobs.values()):
            self.agent.cron.add_job(
                "task_specs_tick", "every_minute", self.run_due_task_specs
            )
        
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="ApiServer")
        self._thread.start()
        print(f"  📡 API Server 启动成功 (http://{self.host}:{self.port})")

        # 启动 Auto-Failover Worker (S-5)
        try:
            from skills.ops_socks5 import start_failover_worker
            start_failover_worker()
        except Exception as e:
            print(f"  ⚠️ 启动 Failover Worker 异常: {str(e)}")

    def stop(self):
        if self.server:
            # shutdown must be called from a different thread to avoid deadlock
            threading.Thread(target=self.server.shutdown).start()
            print("  🛑 API Server 已停止")

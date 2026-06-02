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
    
    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')

    def do_OPTIONS(self):
        self.send_response(200, "ok")
        self._send_cors_headers()
        self.end_headers()

    def _auth(self) -> bool:
        auth_token = self.server.api_server.auth_token
        if not auth_token:
            return True
        auth_header = self.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            self.send_error(401, "Unauthorized")
            return False
        token = auth_header.split(' ')[1]
        if token != auth_token:
            self.send_error(403, "Forbidden")
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
            
        parsed_url = urlparse(self.path)
        if parsed_url.path == '/api/v1/task/stream':
            self._handle_task_stream(parsed_url.query)
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        if not self._auth():
            return
            
        parsed_url = urlparse(self.path)
        if parsed_url.path in ('/api/v1/chat', '/api/v1/task'):
            self._handle_chat_or_task()
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

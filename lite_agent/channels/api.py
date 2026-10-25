import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from types import SimpleNamespace
from typing import Any, Optional, Dict, List

# ---- 会审会议长轮询与限流状态 (供 API 传输与单元测试透明访问) ----
_REVIEW_MAX_WAIT = 25.0      # 秒；须小于 nginx proxy_read_timeout
_REVIEW_ONLINE_WINDOW = 90   # 秒；窗口内该席位有轮询即视为在线
_REVIEW_WAITERS = {}         # (meeting_id, seat) -> True；单席位同时只允许一个挂起
_REVIEW_WAITERS_LOCK = threading.Lock()
_REVIEW_AUTH_ATTEMPTS = {}
_REVIEW_AUTH_LOCK = threading.Lock()
_REVIEW_LAST_POLL = {}       # (meeting_id, seat) -> 最近轮询 epoch（易变状态，不入 audit 链）
_REVIEW_POLL_SLOTS = threading.BoundedSemaphore(64)
_REVIEW_REQUESTS = {}

def _extract_api_server(handler: Any) -> Any:
    srv = getattr(handler, "server", None)
    return getattr(srv, "api_server", None) if srv is not None else None


class ApiHandler(BaseHTTPRequestHandler):
    """
    统一的开放 API 处理器，处理 HTTP 请求。
    """
    def log_message(self, format, *args):
        if getattr(self, '_quiet', False):
            return
        super().log_message(format, *args)

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def _send_cors_headers(self):
        origin = self.headers.get('Origin')
        api = getattr(self.server, "api_server", None)
        cfg = getattr(api, "config", {}) if api else {}
        route_reg = getattr(api, "route_registry", None)

        req_path = urlparse(self.path).path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'

        is_restricted = False
        if route_reg and hasattr(route_reg, 'is_cors_restricted') and route_reg.is_cors_restricted(req_path):
            is_restricted = True
        elif req_path == '/api/v1/auth':
            is_restricted = True

        if is_restricted:
            base = cfg.get('review_meeting_base_url') or f'http://127.0.0.1:{getattr(api, "port", 8080)}'
            parsed = urlparse(base)
            allowed = {cfg.get('review_dashboard_origin', 'https://mail.maifeipin.com')}
            if parsed.scheme and parsed.netloc:
                allowed.add(f'{parsed.scheme}://{parsed.netloc}')
            extra = cfg.get('cors_origins') or []
            allowed.update(str(h).strip() for h in extra if str(h).strip())
            if origin in allowed:
                self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        else:
            self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PATCH, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')

    def do_OPTIONS(self):
        self.send_response(200, "ok")
        self._send_cors_headers()
        self.end_headers()

    def _auth(self) -> bool:
        import os
        api_srv = getattr(self.server, "api_server", None)
        auth_token = getattr(api_srv, "auth_token", "") if api_srv else ""
        guest_token = api_srv.config.get("guest_token", "") if api_srv and hasattr(api_srv, "config") else ""
        # The edge_token is at the root config, so we can check os.environ directly since it's mapped from .env
        edge_token = os.environ.get("EDGE_TOKEN", "")
        session_service = getattr(api_srv, "user_session_service", None)

        self.is_guest = False
        self.is_edge = False
        self.user_session = None

        has_static = bool(auth_token or guest_token or edge_token)
        has_session = bool(session_service and session_service.is_available())

        if not has_static and not has_session:
            self.send_error(503, "API authentication is not configured")
            return False

        auth_header = self.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            self.send_error(401, "Unauthorized")
            return False

        parts = auth_header.split(' ')
        if len(parts) != 2 or not parts[1]:
            self.send_error(401, "Unauthorized")
            return False
        token = parts[1]

        if auth_token and token == auth_token:
            self.is_guest = False
            return True
        elif guest_token and token == guest_token:
            self.is_guest = True
            return True
        elif edge_token and token == edge_token:
            self.is_edge = True
            return True
        elif session_service:
            verified, reason = session_service.verify_access_token(token, required_scope="user:general")
            if verified:
                self.is_guest = False
                self.user_session = verified
                return True
            if reason in ("expired", "revoked"):
                self.send_error(401, "Token expired or revoked")
                return False

        self.send_error(403, "Forbidden")
        return False

    def do_GET(self):
        parsed_url = urlparse(self.path)
        req_path = parsed_url.path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'

        api_srv = _extract_api_server(self)
        route_reg = getattr(api_srv, "route_registry", None) if api_srv else None

        # 1. 允许公开免认证路由 (auth_required=False)
        if route_reg is not None:
            matched = route_reg.match("GET", req_path)
            if matched and not matched.get("auth_required", True):
                matched["handler"](self, parsed_url)
                return

        # 1.5. 应用内一键更新 API（版本检查与 APK 下载，公开免认证）
        if req_path == '/api/v1/app/version':
            self._handle_app_version(parsed_url.query)
            return
        elif req_path == '/api/v1/app/download/latest.apk':
            self._handle_app_download()
            return

        # 2. 仪表盘 API 无需认证（仅返回注册表指令列表，无敏感数据）
        if req_path == '/api/v1/dashboard':
            self._handle_dashboard()
            return

        # 4. 严格执行认证与权限隔离
        if not self._auth():
            return
        if self.is_guest and req_path not in ('/v1/models', '/api/v1/task/stream'):
            self.send_error(403, 'Forbidden: guest access is limited to chat and own task stream')
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

        # 5. 鉴权通过后派发受保护的插件登记路由
        if route_reg is not None:
            matched = route_reg.match("GET", req_path)
            if matched:
                matched["handler"](self, parsed_url)
                return

        if req_path == '/api/pull_task':
            self.send_error(404, "Not Found")
            return
        elif req_path == '/api/v1/sessions':
            self._handle_sessions(parsed_url.query)
        elif req_path == '/api/v1/sessions/messages':
            self._handle_session_messages(parsed_url.query)
        elif req_path == '/api/v1/task/stream':
            self._handle_task_stream(parsed_url.query)
        elif req_path == '/api/v1/task-specs':
            self._handle_task_specs_get(parsed_url.query)
        elif req_path == '/api/v1/task-specs/meta':
            self._handle_task_specs_meta()
        elif req_path.startswith('/api/v1/task-specs/'):
            self._handle_task_spec_get(req_path)
        elif req_path.startswith('/api/v1/output-archive/'):
            self._handle_output_archive_get(req_path)
        elif req_path == '/api/v1/dashboard':
            self._handle_dashboard()
        elif req_path == '/v1/models':
            self._handle_openai_models()
        elif req_path == '/api/v1/mail/recent':
            self._handle_mail_recent(parsed_url.query)
        elif req_path == '/api/v1/billing/summary':
            self._handle_billing_summary()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed_url = urlparse(self.path)
        req_path = parsed_url.path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'

        api_srv = _extract_api_server(self)
        route_reg = getattr(api_srv, "route_registry", None) if api_srv else None

        # 1. 允许公开免认证路由 (auth_required=False)
        if route_reg is not None:
            matched = route_reg.match("POST", req_path)
            if matched and not matched.get("auth_required", True):
                matched["handler"](self, parsed_url)
                return

        # 2. 登录接口无需认证 (若开启了路由注册表且插件未注册，则为未启用会议业务，直接 404)
        if req_path == '/api/v1/auth':
            if route_reg is not None:
                self.send_error(404, "Not Found")
                return
            self._handle_auth()
            return

        # 3. 严格执行认证与权限隔离
        if not self._auth():
            return
        if self.is_guest and req_path not in ('/api/v1/chat', '/api/v1/task', '/v1/chat/completions'):
            self.send_error(403, 'Forbidden: guest access is limited to chat')
            return
        if self.is_guest and req_path.startswith('/api/v1/task-specs'):
            self.send_error(403, "Forbidden: TaskSpec management requires admin")
            return

        # 边缘节点权限隔离：仅允许 /api/report, /api/task_result
        if getattr(self, 'is_edge', False) and req_path not in ('/api/report', '/api/task_result'):
            self.send_error(403, "Forbidden: Edge token is limited to /api/report, /api/task_result")
            return

        # 4. 鉴权通过后派发受保护的插件登记路由
        if route_reg is not None:
            matched = route_reg.match("POST", req_path)
            if matched:
                matched["handler"](self, parsed_url)
                return

        if req_path in ('/api/report', '/api/task_result', '/api/edge_task'):
            self.send_error(404, "Not Found")
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
        elif req_path == '/api/v1/task-specs':
            self._handle_task_spec_create()
        elif req_path == '/api/v1/task-specs/generate':
            self._handle_task_spec_generate()
        elif req_path.startswith('/api/v1/task-specs/'):
            self._handle_task_spec_action(req_path)
        elif req_path == '/api/v1/session/title':
            self._handle_post_session_title()
        else:
            self.send_error(404, "Not Found")

    def do_PATCH(self):
        parsed_url = urlparse(self.path)
        if getattr(self, 'is_edge', False):
            self.send_error(403, "Forbidden")
            return
        req_path = parsed_url.path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'

        api_srv = _extract_api_server(self)
        route_reg = getattr(api_srv, "route_registry", None) if api_srv else None

        # 1. 允许公开免认证路由 (auth_required=False)
        if route_reg is not None:
            matched = route_reg.match("PATCH", req_path)
            if matched and not matched.get("auth_required", True):
                matched["handler"](self, parsed_url)
                return

        if not self._auth():
            return
        if self.is_guest or self.is_edge:
            self.send_error(403, 'Forbidden: admin access required')
            return
        if self.is_guest and req_path.startswith('/api/v1/task-specs'):
            self.send_error(403, "Forbidden: TaskSpec management requires admin")
            return

        if route_reg is not None:
            matched = route_reg.match("PATCH", req_path)
            if matched:
                matched["handler"](self, parsed_url)
                return

        if req_path.startswith('/api/v1/task-specs/'):
            self._handle_task_spec_update(req_path)
        else:
            self.send_error(404, "Not Found")

    def do_DELETE(self):
        parsed_url = urlparse(self.path)
        if getattr(self, 'is_edge', False):
            self.send_error(403, "Forbidden")
            return
        req_path = parsed_url.path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'

        api_srv = _extract_api_server(self)
        route_reg = getattr(api_srv, "route_registry", None) if api_srv else None

        # 1. 允许公开免认证路由 (auth_required=False)
        if route_reg is not None:
            matched = route_reg.match("DELETE", req_path)
            if matched and not matched.get("auth_required", True):
                matched["handler"](self, parsed_url)
                return

        if not self._auth():
            return
        if self.is_guest or self.is_edge:
            self.send_error(403, 'Forbidden: admin access required')
            return
        if self.is_guest and req_path.startswith('/api/v1/task-specs'):
            self.send_error(403, "Forbidden: TaskSpec management requires admin")
            return

        if route_reg is not None:
            matched = route_reg.match("DELETE", req_path)
            if matched:
                matched["handler"](self, parsed_url)
                return

        if req_path.startswith('/api/v1/task-specs/'):
            self._handle_task_spec_delete(req_path)
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


    def _handle_chat_or_task(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            content_length = 0
        if not 0 < content_length <= 65536:
            self.send_error(400, "Bad Request: body must be 1..65536 bytes")
            return

        body = self.rfile.read(content_length)
        try:
            req_data = json.loads(body.decode('utf-8'))
        except json.JSONDecodeError:
            self.send_error(400, "Bad Request: Invalid JSON")
            return

        session_id = req_data.get('session_id')
        text = req_data.get('text')
        if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 128
                or session_id.startswith('guest/') or not isinstance(text, str) or not text.strip()):
            self.send_error(400, "Bad Request: Missing session_id or text")
            return
        if self.is_guest:
            session_id = 'guest/' + session_id

        notify_channels = req_data.get('notify_channels', [])
        if self.is_guest:
            notify_channels = ['api']
        elif not isinstance(notify_channels, list) or any(
                not isinstance(name, str) for name in notify_channels):
            self.send_error(400, "notify_channels must be a list of channel names")
            return

        from lite_agent.agent import IncomingMessage
        msg = IncomingMessage(
            channel='api',
            user_id=session_id,
            chat_id=session_id,
            message_id=str(time.time()),
            text=text,
            notify_channels=notify_channels,
            is_guest=self.is_guest,
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
        Body: {title?, text, color?, dedup_key?, wait_delivery?}
        wait_delivery=true 返回通道发送回执；默认保持先 ACK 后推送。"""
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

        # Todo callers require a delivery receipt, rather than the default ACK.
        if req.get('wait_delivery') is True:
            try:
                from lite_agent.core.alerts import push_alert
                delivered = push_alert(self.server.api_server.agent, text,
                    title=title, color=color, dedup_key=dedup_key)
            except Exception:
                delivered = False
            receipt = json.dumps({'status': 'delivered' if delivered else 'failed'}).encode('utf-8')
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(receipt)))
            self.end_headers()
            self.wfile.write(receipt)
            return

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
            from lite_agent.core.alerts import push_alert
            agent = self.server.api_server.agent
            push_alert(agent, text, title=title, color=color, dedup_key=dedup_key)
        except Exception as e:
            print(f"  ⚠️ [alert] push_alert 异常: {e}")

    def _handle_dashboard(self):
        """返回仪表盘可用的指令列表（来自注册表）。"""
        host = _extract_api_server(self)
        agent = getattr(host, "agent", None)
        registry = getattr(agent, "command_registry", None)
        if registry is None:
            self.send_error(503, "Command registry unavailable")
            return
        items = registry.items_for_dashboard()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(items, ensure_ascii=False).encode('utf-8'))

    def _handle_app_version(self, query: str):
        """检查移动端 App 最新版本信息 (未登录匿名可用)。"""
        from urllib.parse import parse_qs
        from lite_agent.core.constants import PROJECT_ROOT
        qs = parse_qs(query)
        current_code = int(qs.get('currentCode', [0])[0] or 0)
        channel = qs.get('channel', ['debug'])[0]
        pkg = qs.get('packageName', ['com.liteagent.mobile'])[0]

        manifest_path = os.path.join(PROJECT_ROOT, 'data', 'app_version.json')
        version_data = {}
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    version_data = json.load(f)
            except Exception:
                pass

        latest_code = int(version_data.get('versionCode', 100003))
        latest_name = version_data.get('versionName', '1.0.3')
        has_update = latest_code > current_code
        force_update = bool(version_data.get('forceUpdate', False))

        apk_path = os.path.join(PROJECT_ROOT, 'app', 'android', 'app', 'build', 'outputs', 'apk', 'debug', 'app-debug.apk')
        size_bytes = 0
        if os.path.exists(apk_path):
            try:
                size_bytes = os.path.getsize(apk_path)
            except Exception:
                pass

        payload = {
            "hasUpdate": has_update,
            "forceUpdate": force_update,
            "channel": channel,
            "versionCode": latest_code,
            "versionName": latest_name,
            "packageName": pkg,
            "minSupportedVersionCode": version_data.get('minSupportedVersionCode', 1),
            "sizeBytes": size_bytes,
            "sha256": version_data.get('sha256'),
            "changelog": version_data.get('changelog', "Sprint 3: 待办高级提醒、会审轮询与直播滚动、发现中心账单邮件"),
            "publishedAt": version_data.get('publishedAt', "2026-10-25T08:30:00Z"),
            "downloadUrl": version_data.get('downloadUrl', "/agent/api/v1/app/download/latest.apk")
        }
        self._send_json(payload)

    def _handle_app_download(self):
        """流式分发最新 debug/release APK (未登录匿名可用)。"""
        from lite_agent.core.constants import PROJECT_ROOT
        apk_candidates = [
            os.path.join(PROJECT_ROOT, 'app', 'android', 'app', 'build', 'outputs', 'apk', 'debug', 'app-debug.apk'),
            os.path.join(PROJECT_ROOT, 'dist', 'app-debug.apk'),
        ]
        apk_path = None
        for p in apk_candidates:
            if os.path.exists(p):
                apk_path = p
                break

        if not apk_path:
            self.send_error(404, "最新 APK 尚未生成或已归档")
            return

        try:
            size = os.path.getsize(apk_path)
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/vnd.android.package-archive')
            self.send_header('Content-Length', str(size))
            self.send_header('Content-Disposition', 'attachment; filename="liteagent-debug.apk"')
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            with open(apk_path, 'rb') as f:
                while chunk := f.read(65536):
                    self.wfile.write(chunk)
        except Exception as e:
            self.send_error(500, f"分发 APK 失败: {e}")

    def _handle_mail_recent(self, query: str):
        """获取最近邮件摘要列表 (支持移动端快速检索)。"""
        from urllib.parse import parse_qs
        qs = parse_qs(query)
        limit = min(int(qs.get('limit', [20])[0] or 20), 100)
        from lite_agent.integrations.mail.paths import get_statement_db_path
        db_path = get_statement_db_path()
        emails = []
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT es.id, es.account_name, es.uid, es.sender, es.subject,
                           es.email_date, es.importance, es.category, es.summary, es.processed
                    FROM email_summaries es
                    ORDER BY es.email_date DESC, es.id DESC
                    LIMIT ?
                """, (limit,))
                for row in cur.fetchall():
                    emails.append(dict(row))
                conn.close()
            except Exception:
                pass

        self._send_json({"success": True, "emails": emails})

    def _handle_billing_summary(self):
        """获取账单汇总统计与最近账单。"""
        from lite_agent.integrations.mail.paths import get_statement_db_path
        db_path = get_statement_db_path()
        summary = {
            "success": True,
            "total_statements": 0,
            "unpaid_count": 0,
            "total_unpaid_cny": 0.0,
            "recent_statements": [],
            "health_status": "healthy"
        }
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if 'statements' in tables:
                    row_total = cur.execute("SELECT count(*) FROM statements").fetchone()
                    summary["total_statements"] = row_total[0] if row_total else 0

                    row_unpaid = cur.execute(
                        "SELECT count(*), coalesce(sum(amount), 0) FROM statements WHERE paid = 0"
                    ).fetchone()
                    if row_unpaid:
                        summary["unpaid_count"] = row_unpaid[0]
                        summary["total_unpaid_cny"] = float(row_unpaid[1])

                    cur.execute("""
                        SELECT id, bank, card_number, due_date, amount, currency, paid, statement_date
                        FROM statements
                        ORDER BY statement_date DESC, id DESC
                        LIMIT 10
                    """)
                    for r in cur.fetchall():
                        summary["recent_statements"].append(dict(r))
                conn.close()
            except Exception as e:
                summary["health_status"] = f"degraded: {e}"

        self._send_json(summary)

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
        from lite_agent.core.task_spec import BASE_POLICY, SCHEMA_VERSION, policy_digest
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
        from lite_agent.core.output_delivery import get_archived_output
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
        from lite_agent.core.task_spec_service import TaskSpecRevisionConflict

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

        from lite_agent.core.constants import PROJECT_ROOT
        db_path = os.path.join(PROJECT_ROOT, "data", "sessions.db")

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

        from lite_agent.core.constants import PROJECT_ROOT
        db_path = os.path.join(PROJECT_ROOT, "data", "sessions.db")

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
        self.send_error(404, "Not Found")


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


    def _handle_task_stream(self, query: str):
        qs = parse_qs(query)
        task_id = qs.get('task_id', [None])[0]
        session_id = qs.get('session_id', [None])[0]

        if not task_id or not session_id:
            self.send_error(400, "Bad Request: Missing task_id or session_id")
            return
        if session_id.startswith('guest/'):
            self.send_error(400, 'Use the original session_id, not a reserved namespace')
            return
        if self.is_guest:
            session_id = 'guest/' + session_id

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

                    if status in ('done', 'completed', 'succeeded', 'partial', 'failed', 'error'):
                        break
                else:
                    data_obj = {"status": "planning", "message": "正在规划任务..."}
                    self.wfile.write(f"data: {json.dumps(data_obj, ensure_ascii=False)}\n\n".encode('utf-8'))
                    self.wfile.flush()
            except Exception as e:
                # Client probably disconnected
                break

            time.sleep(1)


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
            session_id = f"oai_{'guest' if is_guest_mode else 'admin'}_u_{client_user}"
        else:
            role_name = "guest" if is_guest_mode else "admin"
            session_id = f"oai_{role_name}"
        if is_guest_mode:
            session_id = 'guest/' + session_id

        from lite_agent.agent import IncomingMessage
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



class ApiServer:
    """独立的 API 服务端，专门处理 Web 界面和第三方系统的 REST/SSE 请求"""

    def __init__(self, config: dict, agent):
        self.config = config.get("api", {})
        self.agent = agent
        self.host = self.config.get("host", "0.0.0.0")
        self.port = self.config.get("port", 8080)
        self.auth_token = self.config.get("auth_token", "")
        self.route_registry = getattr(agent, "route_registry", None)
        from lite_agent.services.user_session import UserSessionService
        self.user_session_service = getattr(agent, "user_session_service", None)
        if self.user_session_service is None:
            self.user_session_service = UserSessionService(
                config=getattr(agent, "_config", None) or config
            )
        self.server = None
        self._thread = None
        from lite_agent.core.task_spec_service import TaskSpecService
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
        from lite_agent.core.task_spec import content_digest
        if validated != content_digest(current["spec"]):
            raise ValueError("TaskSpec 内容已变化，需要重新校验")
        with self._task_run_lock:
            if task_id in self._running_task_specs:
                raise ValueError("TaskSpec 正在执行")
            self._running_task_specs.add(task_id)
        self.task_specs.store.mark_started(task_id, scheduled=scheduled)

        def _run():
            try:
                from lite_agent.core.task_orchestrator import TaskOrchestrator
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
        self.port = self.server.server_address[1]

        if not any(job.name == "task_specs_tick" for job in self.agent.cron.jobs.values()):
            self.agent.cron.add_job(
                "task_specs_tick", "every_minute", self.run_due_task_specs
            )

        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="ApiServer")
        self._thread.start()
        print(f"  📡 API Server 启动成功 (http://{self.host}:{self.port})")

    def stop(self):
        if self.server:
            # shutdown must be called from a different thread to avoid deadlock
            threading.Thread(target=self.server.shutdown).start()
            print("  🛑 API Server 已停止")

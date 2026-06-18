import urllib.request, json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor
from channels.base import BaseChannel


class WeComChannel(BaseChannel):
    """企业微信通道 — 复用 pushmsg 服务 (port 6969) 发送，内置 HTTP 接收 w.py 转发"""

    def __init__(self, config: dict, agent):
        super().__init__('wecom', config, agent)
        self.push_url = config.get('push_url', 'http://127.0.0.1:6969/send_message')
        self.push_token = config.get('push_token', '')
        self.listen_port = config.get('listen_port', 8899)
        self._httpd = None
        self.executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="WeComWorker")

    def start(self):
        self._httpd = HTTPServer(('127.0.0.1', self.listen_port), _make_handler(self))
        self.executor.submit(self._httpd.serve_forever)
        print(f"  📡 企业微信通道就绪 (收: :{self.listen_port}, 发: {self.push_url})")

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()

    def send_response(self, message_id: str, response) -> bool:
        user_id = self._parse_user_id(message_id)
        if not user_id:
            print(f"  ❌ [WeCom] send_response 无法解析 user_id from {message_id!r}, 拒绝 fallback @all")
            return False
        text = f"**{response.title}**\n\n{response.text}" if response.title else response.text
        return self._do_send(text, use_md=bool(response.title), user_id=user_id)

    def send_to(self, open_id: str, response) -> bool:
        if not open_id:
            print("  ❌ [WeCom] send_to 收到空 open_id, 拒绝 fallback @all")
            return False
        text = f"**{response.title}**\n\n{response.text}" if response.title else response.text
        return self._do_send(text, use_md=bool(response.title), user_id=open_id)

    def send_progress(self, message_id: str, text: str = "") -> bool:
        """收到消息后/编排进度回调，发一条 markdown 状态卡片。
        失败仅打日志，不抛异常给 agent。
        无法解析 user_id 时 fail-closed: 返回 False, 不退化为 @all 群发。"""
        try:
            user_id = self._parse_user_id(message_id)
            if not user_id:
                print(f"  ❌ [WeCom] send_progress 无法解析 user_id from {message_id!r}, 拒绝 fallback @all")
                return False
            from channels import smart_truncate
            body = smart_truncate(text or "已收到，AI 正在分析中...", 3500)
            return self._do_send(f"🤔 **处理中**\n\n{body}", use_md=True, user_id=user_id)
        except Exception as e:
            print(f"  ⚠️ [WeCom] send_progress 异常: {e}")
            return False

    def broadcast(self, response) -> bool:
        """从会话库中查询所有活跃 wecom 用户并主动广播。
        依赖 vps1 上的 pushmsg 服务支持 'touser' 字段。"""
        user_ids = []
        try:
            with self.agent.session_mgr._connect() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT session_key FROM sessions WHERE session_key LIKE 'wecom:%'"
                ).fetchall()
                for r in rows:
                    uid = r[0].split(':', 1)[1]
                    if uid:
                        user_ids.append(uid)
        except Exception as e:
            print(f"❌ [WeCom] 广播查询用户失败: {e}")
            return False

        if not user_ids:
            print("📣 [WeCom] 广播无活跃用户，跳过")
            return False

        text = f"**{response.title}**\n\n{response.text}" if response.title else response.text
        success_count = 0
        for uid in user_ids:
            if self._do_send(text, use_md=bool(response.title), user_id=uid):
                success_count += 1

        print(f"📣 [WeCom] 广播完成，成功发送 {success_count}/{len(user_ids)} 人")
        return success_count > 0

    def _parse_user_id(self, message_id: str):
        """从 _feed_message 生成的 message_id 中解析 user_id。
        格式: wecom_<user_id>_<ts_ms>。无法解析时返回 None（pushmsg 自动 @all 兜底）。"""
        if not message_id or not message_id.startswith('wecom_'):
            return None
        # rsplit 一次，把最后一段时间戳剔除；user_id 可含下划线，从右侧切更安全
        body = message_id[len('wecom_'):]
        parts = body.rsplit('_', 1)
        if len(parts) != 2 or not parts[1].isdigit():
            return None
        return parts[0] or None

    def _do_send(self, text: str, use_md: bool = False, user_id: str = None) -> bool:
        try:
            payload = {'message': text}
            if use_md:
                payload['msgtype'] = 'markdown'
            if user_id:
                payload['touser'] = user_id
            data = json.dumps(payload).encode('utf-8')
            headers = {'Content-Type': 'application/json'}
            if self.push_token:
                headers['Authorization'] = f'Bearer {self.push_token}'
            req = urllib.request.Request(self.push_url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status == 200
        except Exception as e:
            target = user_id or '@all'
            print(f"  ❌ 企业微信推送失败 (touser={target}): {e}")
            return False

    def _feed_message(self, text: str, user_id: str):
        from agent import IncomingMessage
        import time

        if not text or not text.strip():
            return

        msg_id = text.strip()[:80]
        # 防重放机制
        if self.agent.session_mgr.is_message_processed(f"wecom_{msg_id}"):
            return

        print(f"📩 [wecom] {user_id}: {text.strip()[:80]}")

        text_stripped = text.strip()
        from channels import smart_truncate
        self._do_send(f"🤔 已收到: _{smart_truncate(text_stripped, 60)}_", use_md=True, user_id=user_id)

        admin_id = self.config.get('admin_userid')
        is_guest = False
        if admin_id:
            if user_id != str(admin_id):
                is_guest = True
        else:
            print("⚠️ [WeCom] admin_userid is not configured! All incoming users will have full admin rights.")

        msg = IncomingMessage(
            channel='wecom',
            user_id=user_id,
            chat_id=user_id,
            # 把 user_id 编进 message_id，便于 send_progress / send_response 反向路由 touser
            message_id=f'wecom_{user_id}_{int(time.time()*1000)}',
            text=text_stripped,
            is_guest=is_guest
        )
        try:
            response = self.agent.handle(msg)
            self.send_response(msg.message_id, response)
        except Exception as e:
            self._do_send(f"❌ 处理失败: {e}", use_md=True, user_id=user_id)
            print(f"  ❌ 企业微信消息处理异常: {e}")


def _make_handler(channel):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
                text = data.get('text', '').strip()
                user_id = data.get('user', 'unknown')
                if text:
                    # 将实际处理丢入线程池，避免阻塞 HTTP 响应
                    channel.executor.submit(channel._feed_message, text, user_id)
            except Exception as e:
                print(f"  ⚠️ 企业微信消息解析失败: {e}")
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass
    return Handler

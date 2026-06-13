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
        text = f"**{response.title}**\n\n{response.text}" if response.title else response.text
        return self._do_send(text, use_md=bool(response.title))

    def send_to(self, open_id: str, response) -> bool:
        return self.send_response('', response)

    def _do_send(self, text: str, use_md: bool = False) -> bool:
        try:
            payload = {'message': text}
            if use_md:
                payload['msgtype'] = 'markdown'
            data = json.dumps(payload).encode('utf-8')
            headers = {'Content-Type': 'application/json'}
            if self.push_token:
                headers['Authorization'] = f'Bearer {self.push_token}'
            req = urllib.request.Request(self.push_url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status == 200
        except Exception as e:
            print(f"  ❌ 企业微信推送失败: {e}")
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
        self._do_send(f"🤔 已收到: _{smart_truncate(text_stripped, 60)}_", use_md=True)

        msg = IncomingMessage(
            channel='wecom',
            user_id=user_id,
            chat_id=user_id,
            message_id=f'wecom_{int(time.time()*1000)}',
            text=text_stripped,
        )
        try:
            response = self.agent.handle(msg)
            self.send_response(msg.message_id, response)
        except Exception as e:
            self._do_send(f"❌ 处理失败: {e}", use_md=True)
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

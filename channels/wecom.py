import urllib.request, json
from channels.base import BaseChannel


class WeComChannel(BaseChannel):
    """企业微信通道 — 复用 pushmsg 服务 (port 6969) 发送，接收由 w.py 处理"""

    def __init__(self, config: dict, agent):
        super().__init__('wecom', config, agent)
        self.push_url = config.get('push_url', 'http://127.0.0.1:6969/send_message')
        self.push_token = config.get('push_token', '')

    def start(self):
        print(f"  📡 企业微信通道就绪 (push: {self.push_url})")

    def stop(self):
        pass

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

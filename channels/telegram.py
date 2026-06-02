import json, time, subprocess, threading
from channels.base import BaseChannel
from agent import IncomingMessage, AgentResponse


class TelegramChannel(BaseChannel):
    """Telegram 通道 — Long Polling via subprocess+curl (socks5h)"""

    def __init__(self, config: dict, agent):
        super().__init__('telegram', config, agent)
        self.bot_token = config.get('bot_token', '')
        self.proxy = config.get('proxy', 'socks5h://127.0.0.1:18988')
        self.base_url = f'https://api.telegram.org/bot{self.bot_token}'
        self.running = False
        self.offset = 0

    def _curl(self, method: str, data: dict = None) -> dict:
        url = f'{self.base_url}/{method}'
        cmd = ['curl', '-x', self.proxy, '-k', '-s', '-m', '40', url]
        if data:
            cmd += ['-H', 'Content-Type: application/json', '-d', json.dumps(data)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            if r.stdout.strip():
                return json.loads(r.stdout)
        except Exception as e:
            print(f'  ❌ [Telegram] {method} error: {e}')
        return {}

    def _poll_loop(self):
        print(f'  📡 Telegram 通道就绪 (Long Polling @ {self.proxy})')
        while self.running:
            updates = self._curl('getUpdates', {
                'offset': self.offset, 'timeout': 30,
                'allowed_updates': ['message']
            })
            if not updates.get('ok'):
                time.sleep(2)
                continue

            for upd in updates.get('result', []):
                self.offset = upd['update_id'] + 1
                msg = upd.get('message')
                if not msg or 'text' not in msg:
                    continue

                chat_id = str(msg['chat']['id'])
                text = msg['text']
                msg_id = str(msg['message_id'])

                incoming = IncomingMessage(
                    channel='telegram', user_id=chat_id, chat_id=chat_id,
                    message_id=f'{chat_id}_{msg_id}', text=text,
                )
                resp = self.agent.handle(incoming)
                if resp:
                    self.send_response(chat_id, resp)

    def start(self):
        if not self.bot_token:
            print('  ⚠️ Telegram token 未配置')
            return
        self.running = True
        threading.Thread(target=self._poll_loop, daemon=True, name='TG').start()

    def stop(self):
        self.running = False

    def send_response(self, chat_id: str, resp: AgentResponse) -> bool:
        text = resp.text
        if resp.title:
            text = f'**{resp.title}**\n\n{text}'
        return self._send_msg(chat_id, text)

    def send_to(self, chat_id: str, resp: AgentResponse) -> bool:
        return self.send_response(chat_id, resp)

    def _send_msg(self, chat_id: str, text: str) -> bool:
        r = self._curl('sendMessage', {
            'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'
        })
        if not r.get('ok'):
            r = self._curl('sendMessage', {
                'chat_id': chat_id, 'text': text
            })
        return bool(r.get('ok'))

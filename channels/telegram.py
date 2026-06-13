import json, time, subprocess, threading
from concurrent.futures import ThreadPoolExecutor
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
        self.executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="TelegramWorker")

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
                if not msg:
                    continue
                
                chat_id = str(msg['chat']['id'])
                msg_id = str(msg['message_id'])

                # 防重放机制
                telegram_msg_id = f'{chat_id}_{msg_id}'
                if self.agent.session_mgr.is_message_processed(telegram_msg_id):
                    continue

                if 'photo' in msg:
                    self.executor.submit(self._process_photo, chat_id, msg_id, msg['photo'])
                    continue

                if 'text' not in msg:
                    continue

                text = msg['text']

                incoming = IncomingMessage(
                    channel='telegram', user_id=chat_id, chat_id=chat_id,
                    message_id=telegram_msg_id, text=text,
                )
                
                # 异步处理文本消息，避免阻塞 polling
                def _handle_and_reply(inc: IncomingMessage, c_id: str):
                    resp = self.agent.handle(inc)
                    if resp:
                        self.send_response(c_id, resp)
                
                self.executor.submit(_handle_and_reply, incoming, chat_id)

    def _process_photo(self, chat_id, msg_id, photo_array):
        self._send_msg(chat_id, "🤔 收到图片，正在调用视觉大模型进行全版面结构化解析...")
        try:
            file_id = photo_array[-1]['file_id']
            file_info = self._curl('getFile', {'file_id': file_id})
            if not file_info.get('ok'):
                self._send_msg(chat_id, "❌ 获取图片信息失败")
                return
            
            file_path = file_info['result']['file_path']
            url = f'https://api.telegram.org/file/bot{self.bot_token}/{file_path}'
            
            import subprocess, requests, os
            cmd = ['curl', '-x', self.proxy, '-k', '-s', url]
            r = subprocess.run(cmd, capture_output=True)
            image_bytes = r.stdout
            if not image_bytes:
                self._send_msg(chat_id, "❌ 下载图片失败")
                return
            
            ocr_url = os.environ.get('OCR_ENDPOINT', 'http://127.0.0.1:8000/api/ocr')
            files = {'file': ('image.jpg', image_bytes, 'image/jpeg')}
            res = requests.post(ocr_url, files=files)
            
            if res.status_code == 200:
                data = res.json()
                markdown = data.get('markdown', '')
                if not markdown:
                    self._send_msg(chat_id, "解析完毕，图片中未识别到文本或公式")
                    return
                self._send_msg(chat_id, f"📄 **视觉模型提取结果**:\n\n{markdown[:4000]}")
            else:
                self._send_msg(chat_id, f"❌ OCR 服务异常: {res.text}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_msg(chat_id, f"❌ 处理图片异常: {e}")

    def start(self):
        if not self.bot_token:
            print('  ⚠️ Telegram token 未配置')
            return
        self.running = True
        self.executor.submit(self._poll_loop)

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

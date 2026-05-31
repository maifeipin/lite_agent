import threading
import json
from channels.base import BaseChannel
from agent import IncomingMessage, AgentResponse

class DingTalkChannel(BaseChannel):
    """
    钉钉 Stream 模式通道
    需要: pip install dingtalk-stream
    """

    def __init__(self, config: dict, agent):
        super().__init__('dingtalk', config, agent)
        self.client_id = config.get('client_id', '')
        self.client_secret = config.get('client_secret', '')
        self.running = False
        self.client = None

    def _on_dingtalk_message(self, message):
        """钉钉 Stream SDK 的回调"""
        try:
            msg_data = message.data if isinstance(message.data, dict) else json.loads(message.data)
            sender_id = msg_data.get('senderStaffId') or msg_data.get('senderId')
            text = msg_data.get('text', {}).get('content', '').strip()
            msg_id = msg_data.get('msgId')
            
            # 如果是群聊，前缀可能包含 @机器人
            if text.startswith('@'):
                parts = text.split(' ', 1)
                if len(parts) > 1:
                    text = parts[1].strip()

            incoming = IncomingMessage(
                channel='dingtalk',
                user_id=sender_id,
                chat_id='',
                message_id=msg_id,
                text=text
            )

            resp = self.agent.handle(incoming)
            if resp:
                # 钉钉回复可以直接调 Webhook URL 或者利用 OpenAPI
                # 简单起见，如果使用 stream，我们可以回复消息
                self.send_response(msg_data, resp)
                
        except Exception as e:
            print(f"❌ [DingTalk] 处理消息失败: {e}")

    def start(self):
        if not self.client_id or not self.client_secret:
            print("⚠️ 钉钉 client_id 或 client_secret 未配置，通道跳过启动。")
            return
            
        try:
            from dingtalk_stream import DingTalkStreamClient, Credential, CallbackHandler, AckMessage
            self.client = DingTalkStreamClient(Credential(self.client_id, self.client_secret))
            
            # 使用官方要求的 CallbackHandler 子类
            class MessageHandler(CallbackHandler):
                def __init__(self, callback):
                    super().__init__()
                    self.callback = callback
                    
                async def process(self, message):
                    self.callback(message)
                    return AckMessage.STATUS_OK, 'ok'
                    
            self.client.register_callback_handler('/v1.0/im/bot/messages/get', MessageHandler(self._on_dingtalk_message))
            
            # 在后台线程启动 (使用 start_forever 因为它是同步阻塞接口)
            self.running = True
            threading.Thread(target=self.client.start_forever, daemon=True, name="DingTalk_Stream").start()
            print("🚀 钉钉 WebSocket (Stream) 通道已启动")
        except ImportError:
            print("❌ [DingTalk] 缺少依赖！请执行: pip install dingtalk-stream")
        except Exception as e:
            print(f"❌ [DingTalk] 启动失败: {e}")

    def stop(self):
        self.running = False
        # DingTalkStreamClient 没有直接的 stop 接口，随守护线程退出即可

    def send_response(self, msg_data: dict, resp: AgentResponse) -> bool:
        # 钉钉官方 SDK (dingtalk-stream) 中没有包含主动回复的 API，
        # 我们需要通过 OpenAPI 获取 token 并调用回复。
        # 简单实现：使用 urllib
        import urllib.request
        try:
            # 1. 取 Access Token
            req = urllib.request.Request(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                data=json.dumps({"appKey": self.client_id, "appSecret": self.client_secret}).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                token_data = json.loads(r.read().decode())
                access_token = token_data.get("accessToken")
            
            if not access_token:
                return False
                
            # 2. 发送回复
            text = f"**{resp.title}**\n\n{resp.text}" if resp.title else resp.text
            reply_data = {
                "msgParam": json.dumps({"content": text}),
                "msgKey": "sampleMarkdown",
            }
            webhook = msg_data.get("sessionWebhook")
            if webhook:
                # 机器人单聊/群聊自带 webhook
                req = urllib.request.Request(
                    webhook,
                    data=json.dumps({"msgtype": "markdown", "markdown": {"title": resp.title or "回复", "text": text}}).encode('utf-8'),
                    headers={'Content-Type': 'application/json', 'x-acs-dingtalk-access-token': access_token}
                )
                with urllib.request.urlopen(req, timeout=10):
                    pass
                return True
        except Exception as e:
            print(f"❌ [DingTalk] 回复失败: {e}")
            return False
        return False

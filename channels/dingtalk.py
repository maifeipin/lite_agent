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
            print(f"📩 [dingtalk] {sender_id}: {text}")
            msg_id = msg_data.get('msgId')
            
            # 如果是群聊，前缀可能包含 @机器人
            if text.startswith('@'):
                parts = text.split(' ', 1)
                if len(parts) > 1:
                    text = parts[1].strip()

            admin_id = self.config.get('admin_staff_id') or self.config.get('admin_userid')
            is_guest = False
            if admin_id:
                if sender_id != str(admin_id):
                    is_guest = True
            else:
                print("⚠️ [DingTalk] admin_staff_id is not configured! All incoming users will have full admin rights.")

            incoming = IncomingMessage(
                channel='dingtalk',
                user_id=sender_id,
                chat_id='',
                message_id=msg_id,
                text=text,
                is_guest=is_guest,
                channel_payload={'msg_data': msg_data}  # 供异步 push_result 取 sessionWebhook
            )

            from channels import smart_truncate
            self.send_progress(msg_data, f"已收到 \"{smart_truncate(text, 50)}\"")
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

    def send_progress(self, msg_data: dict, text: str = "") -> bool:
        """收到消息后立即发送进度反馈"""
        import urllib.request
        try:
            req = urllib.request.Request(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                data=json.dumps({"appKey": self.client_id, "appSecret": self.client_secret}).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                access_token = json.loads(r.read().decode()).get("accessToken")

            if not access_token:
                return False

            progress_text = text or "已收到你的消息，AI 正在分析中..."
            webhook = msg_data.get("sessionWebhook")
            if webhook:
                req = urllib.request.Request(
                    webhook,
                    data=json.dumps({
                        "msgtype": "markdown",
                        "markdown": {"title": "正在处理", "text": f"🤔 **正在处理...**\n\n{progress_text}"}
                    }).encode('utf-8'),
                    headers={'Content-Type': 'application/json', 'x-acs-dingtalk-access-token': access_token}
                )
                with urllib.request.urlopen(req, timeout=10):
                    pass
                return True
        except Exception as e:
            print(f"  ⚠️ [DingTalk] 进度发送失败: {e}")
        return False

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

    def push_result(self, msg, response: AgentResponse) -> bool:
        """异步推送 DAG 编排结果 (修复: 用 channel_payload 里的 msg_data 取 sessionWebhook,
        而非把 message_id(str) 当 dict 传给 send_response)。"""
        msg_data = (msg.channel_payload or {}).get('msg_data')
        if not msg_data:
            print("  ⚠️ [DingTalk] push_result 缺少 msg_data (channel_payload 未携带), 无法异步推送")
            return False
        return self.send_response(msg_data, response)

    def broadcast(self, response: AgentResponse) -> bool:
        """从会话库中查询所有活跃钉钉用户并主动广播 (oToMessages/batchSend)"""
        user_ids = []
        try:
            with self.agent.session_mgr._connect() as conn:
                rows = conn.execute("SELECT DISTINCT session_key FROM sessions WHERE session_key LIKE 'dingtalk:%'").fetchall()
                for r in rows:
                    uid = r[0].split(':', 1)[1]
                    if uid: user_ids.append(uid)
        except Exception as e:
            print(f"❌ 广播查询用户失败: {e}")
            return False

        if not user_ids:
            return False
            
        import urllib.request
        try:
            # 获取 Access Token
            req = urllib.request.Request(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                data=json.dumps({"appKey": self.client_id, "appSecret": self.client_secret}).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                access_token = json.loads(r.read().decode()).get("accessToken")
            
            if not access_token: return False
            
            text = f"**{response.title}**\n\n{response.text}" if response.title else response.text
            msg_param = json.dumps({"title": response.title or "系统广播", "text": text})
            
            req = urllib.request.Request(
                "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                data=json.dumps({
                    "robotCode": self.client_id,
                    "userIds": user_ids,
                    "msgKey": "sampleMarkdown",
                    "msgParam": msg_param
                }).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'x-acs-dingtalk-access-token': access_token}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                res_data = json.loads(r.read().decode())
                process_query_key = res_data.get("processQueryKey")
                print(f"📣 [DingTalk] 广播已投递，批次任务: {process_query_key}, 目标人数: {len(user_ids)}")
                return True
        except Exception as e:
            print(f"❌ [DingTalk] 广播失败: {e}")
            return False

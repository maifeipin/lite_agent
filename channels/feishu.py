import json
import re
import threading
import traceback
from datetime import datetime
from collections import deque

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

from channels.base import BaseChannel
from agent import IncomingMessage, AgentResponse

MAX_CARD_LEN = 2800

class FeishuChannel(BaseChannel):
    """飞书 WebSocket 通道实现"""

    def __init__(self, config: dict, agent):
        super().__init__('feishu', config, agent)
        self.app_id = config.get('app_id', '')
        self.app_secret = config.get('app_secret', '')
        # 消息防重放缓存
        self._processed_ids = deque(maxlen=1000)
        
        # 构建飞书客户端
        self.lark_client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .build()
        
        self.ws_client = None
    
    def start(self):
        """启动飞书 WebSocket 长连接"""
        if not self.app_id or not self.app_secret:
            print("⚠️ 飞书通道配置缺失 app_id 或 app_secret，跳过启动")
            return

        event_handler = (
            lark.EventDispatcherHandler.builder('', '')
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        self.ws_client = (
            lark.ws.Client(
                self.app_id,
                self.app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO,
            )
        )
        print("🚀 飞书 WebSocket 通道已启动")
        self.ws_client.start()
    
    def stop(self):
        # 当前 SDK 的 WebSocket 客户端没有显式的停止方法
        pass
    def _on_message(self, data):
        try:
            message = data.event.message
            sender = data.event.sender
            msg_id = message.message_id
            
            # 防重放机制：如果收到了重复的消息 ID，直接丢弃
            if msg_id in self._processed_ids:
                return
            self._processed_ids.append(msg_id)
            
            # 忽略非用户消息，防止机器人互聊死循环
            if sender.sender_type and sender.sender_type != 'user':
                return
            
            # 仅支持文本消息
            if message.message_type != 'text':
                self._reply_card(msg_id, '⚠️ 不支持的消息类型', '当前仅支持文本消息', 'grey')
                return
            
            # 提取清洗后的文本
            text = self._extract_text(message)
            if not text:
                return
            
            sender_id = ''
            if sender.sender_id:
                sender_id = sender.sender_id.open_id or 'unknown'
            
            chat_type = message.chat_type or 'p2p'
            print(f"📩 [feishu:{chat_type}] {sender_id}: {text}")
            
            # 构建标准化的消息对象
            incoming = IncomingMessage(
                channel='feishu',
                user_id=sender_id,
                chat_id=message.chat_id or '',
                message_id=msg_id,
                text=text
            )
            
            # 异步处理消息，避免阻塞导致飞书重传
            threading.Thread(
                target=self._process_and_reply,
                args=(incoming,),
                daemon=True
            ).start()
        
        except Exception as e:
            print(f"❌ 飞书消息处理异常: {e}")
            traceback.print_exc()
    
    def _process_and_reply(self, msg: IncomingMessage):
        """在独立线程中执行 Agent 逻辑并回复"""
        try:
            response = self.agent.handle(msg)
            self._reply_card(msg.message_id, response.title, response.text, response.color)
        except Exception as e:
            print(f"❌ Agent 处理异常: {e}")
            traceback.print_exc()
            self._reply_card(msg.message_id, '❌ 内部错误', f'处理消息时发生异常:\\n`{e}`', 'red')
    
    def _extract_text(self, message) -> str:
        """从飞书消息内容中提取纯文本，清理 @mention 和引用前缀"""
        try:
            content = json.loads(message.content)
            text = content.get('text', '').strip()
        except (json.JSONDecodeError, AttributeError):
            return ''
        
        # 移除群聊 @bot 的占位符
        text = re.sub(r'@_user_\\d+\\s*', '', text).strip()
        text = re.sub(r'@_all\\s*', '', text).strip()
        
        # 移除引用的“回复 xxx：”前缀
        text = re.sub(r'^回复\\s*.*?:[ \\t]*\\n?', '', text).strip()
        
        # 兼容全角斜杠
        text = text.replace('／', '/')
        
        return text
    
    def send_response(self, message_id: str, response: AgentResponse) -> bool:
        """实现基类的发送方法"""
        self._reply_card(message_id, response.title, response.text, response.color)
        return True
    
    def _reply_card(self, message_id: str, title: str, content: str, color: str = 'blue'):
        """向飞书发送富文本卡片"""
        card_json = self._build_card(title, content, color)
        try:
            request = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type('interactive')
                    .content(card_json)
                    .build()
                )
                .build()
            )
            response = self.lark_client.im.v1.message.reply(request)
            if not response.success():
                print(f"  ⚠️ 卡片发送失败: {response.code}, {response.msg}")
            else:
                print(f"  ✅ 卡片已发送")
        except Exception as e:
            print(f"  ❌ 发送卡片异常: {e}")
    
    def _build_card(self, title: str, content: str, color: str = 'blue') -> str:
        """构建飞书卡片 JSON"""
        color_map = {
            'blue': 'blue', 'red': 'red', 'green': 'green', 'orange': 'orange',
            'turquoise': 'turquoise', 'violet': 'violet', 'grey': 'grey',
            'indigo': 'indigo', 'wathet': 'wathet', 'yellow': 'yellow'
        }
        template_color = color_map.get(color, 'blue')
        
        elements = []
        if content:
            if len(content) > MAX_CARD_LEN:
                content = content[:MAX_CARD_LEN] + '\\n\\n... ✂️ 内容过长已截断'
            
            # 将输出内容包裹在 Markdown 代码块中，防止 `*` 等符号被错误解析
            if '```' not in content:
                content_md = f'```\\n{content}\\n```'
            else:
                content_md = content
            
            elements.append({'tag': 'div', 'text': {'tag': 'lark_md', 'content': content_md}})
        
        elements.append({'tag': 'hr'})
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        elements.append({
            'tag': 'note',
            'elements': [{'tag': 'plain_text', 'content': f'🤖 Lite Agent · {ts}'}]
        })
        
        card = {
            'header': {
                'title': {'tag': 'plain_text', 'content': title or '🤖 回复'},
                'template': template_color
            },
            'elements': elements
        }
        return json.dumps(card, ensure_ascii=False)

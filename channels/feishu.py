import json
import os
import re
import threading
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

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
        # 消息防重放机制交由 session_mgr 持久化处理
        
        # 构建线程池执行器
        self.executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="FeishuWorker")
        
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
        self.executor.submit(self.ws_client.start)
    
    def stop(self):
        # 当前 SDK 的 WebSocket 客户端没有显式的停止方法
        pass
    def _on_message(self, data):
        try:
            message = data.event.message
            sender = data.event.sender
            msg_id = message.message_id
            
            # 防重放机制：如果收到了重复的消息 ID，直接丢弃
            if self.agent.session_mgr.is_message_processed(msg_id):
                return
            
            # 忽略非用户消息，防止机器人互聊死循环
            if sender.sender_type and sender.sender_type != 'user':
                return
            
            sender_id = ''
            if sender.sender_id:
                sender_id = sender.sender_id.open_id or 'unknown'
            
            chat_type = message.chat_type or 'p2p'

            # 兼容图片消息并执行 OCR 提取
            if message.message_type == 'image':
                content = json.loads(message.content)
                image_key = content.get('image_key')
                if image_key:
                    self.executor.submit(self._process_image_and_reply, msg_id, sender, message, image_key)
                return

            # 其他类型
            if message.message_type != 'text':
                self._reply_card(msg_id, '⚠️ 不支持的消息类型', '当前仅支持文本消息或图片', 'grey')
                return
            
            # 提取清洗后的文本
            text = self._extract_text(message)
            if not text:
                return
            
            print(f"📩 [feishu:{chat_type}] {sender_id}: {text}")
            
            admin_id = self.config.get('admin_open_id')
            is_guest = False
            if admin_id:
                if sender_id != str(admin_id):
                    is_guest = True
            else:
                print("⚠️ [Feishu] admin_open_id is not configured! All incoming users will have full admin rights.")

            # 构建标准化的消息对象
            incoming = IncomingMessage(
                channel='feishu',
                user_id=sender_id,
                chat_id=message.chat_id or '',
                message_id=msg_id,
                text=text,
                is_guest=is_guest
            )
            
            # 异步处理消息，避免阻塞导致飞书重传
            self.executor.submit(self._process_and_reply, incoming)
        
        except Exception as e:
            print(f"❌ 飞书消息处理异常: {e}")
            traceback.print_exc()
    
    def _process_image_and_reply(self, msg_id, sender, message, image_key):
        """处理图片：通过 OCR API 提取文本并返回"""
        import requests
        self.send_progress(msg_id, "收到图片，正在调用视觉大模型进行全版面结构化解析...")
        try:
            request = (
                lark.api.im.v1.GetMessageResourceRequest.builder()
                .message_id(msg_id)
                .file_key(image_key)
                .type('image')
                .build()
            )
            response = self.lark_client.im.v1.message_resource.get(request)
            
            if not response.success():
                self._reply_card(msg_id, "解析失败", f"无法下载图片: {response.msg}", "red")
                return
                
            image_bytes = response.file.read()
            ocr_url = os.environ.get('OCR_ENDPOINT', 'http://127.0.0.1:8000/api/ocr')
            files = {'file': ('image.jpg', image_bytes, 'image/jpeg')}
            res = requests.post(ocr_url, files=files)
            
            if res.status_code == 200:
                data = res.json()
                markdown = data.get('markdown', '')
                if not markdown:
                    self._reply_card(msg_id, "解析完毕", "图片中未识别到文本或公式", "grey")
                    return
                self._reply_card(msg_id, "📄 视觉模型提取结果", markdown[:MAX_CARD_LEN], "blue")
            else:
                self._reply_card(msg_id, "解析异常", f"OCR 服务返回错误: {res.text}", "red")
        except Exception as e:
            traceback.print_exc()
            self._reply_card(msg_id, "❌ 图片处理异常", str(e), "red")

    def _process_and_reply(self, msg: IncomingMessage):
        """在独立线程中执行 Agent 逻辑并回复"""
        try:
            from channels import smart_truncate
            self.send_progress(msg.message_id, f"已收到 \"{smart_truncate(msg.text, 50)}\"")
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
    
    def send_progress(self, message_id: str, text: str = "") -> bool:
        """收到消息后立即发送进度反馈卡片"""
        self._reply_card(message_id, "🤔 正在处理...", text or "已收到你的消息，AI 正在分析中...", "grey")
        return True

    def send_response(self, message_id: str, response: AgentResponse) -> bool:
        """实现基类的发送方法"""
        self._reply_card(message_id, response.title, response.text, response.color)
        return True

    def send_to(self, receive_id: str, response: AgentResponse) -> bool:
        """主动发送消息到指定 receive_id。

        receive_id 可能是 群会话id (oc_...) 或 用户open_id (ou_...), 根据前缀选 receive_id_type。
        (修复: 原硬编码 open_id, 导致 _push_result 传群 chat_id(oc_) 时飞书 API 报 invalid receive_id)
        """
        if receive_id and receive_id.startswith('oc_'):
            receive_id_type = "chat_id"
        else:
            receive_id_type = "open_id"
        card_json = self._build_card(response.title, response.text, response.color)
        try:
            request = (
                lark.api.im.v1.CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    lark.api.im.v1.CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("interactive")
                    .content(card_json)
                    .build()
                )
                .build()
            )
            res = self.lark_client.im.v1.message.create(request)
            return res.success()
        except Exception as e:
            print(f"  ❌ 飞书发送给 {receive_id} 失败: {e}")
            return False
    
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
            if '```' not in content and '**' not in content and '⭐' not in content:
                content_md = f'```\n{content}\n```'
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

    def broadcast(self, response: AgentResponse) -> bool:
        """从会话库中查询所有活跃飞书用户并主动广播"""
        user_ids = []
        try:
            with self.agent.session_mgr._connect() as conn:
                rows = conn.execute("SELECT DISTINCT session_key FROM sessions WHERE session_key LIKE 'feishu:%'").fetchall()
                for r in rows:
                    uid = r[0].split(':', 1)[1]
                    if uid: user_ids.append(uid)
        except Exception as e:
            print(f"❌ 广播查询用户失败: {e}")
            return False

        if not user_ids:
            return False
            
        card_json = self._build_card(response.title, response.text, response.color)
        success_count = 0
        for uid in user_ids:
            try:
                request = (
                    lark.api.im.v1.CreateMessageRequest.builder()
                    .receive_id_type("open_id")
                    .request_body(
                        lark.api.im.v1.CreateMessageRequestBody.builder()
                        .receive_id(uid)
                        .msg_type("interactive")
                        .content(card_json)
                        .build()
                    )
                    .build()
                )
                res = self.lark_client.im.v1.message.create(request)
                if res.success():
                    success_count += 1
            except Exception as e:
                print(f"  ❌ 飞书广播给 {uid} 失败: {e}")
                
        print(f"📣 [Feishu] 广播完成，成功发送 {success_count}/{len(user_ids)} 人")
        return success_count > 0

from channels.base import BaseChannel

class TelegramChannel(BaseChannel):
    """
    Telegram Bot 通道（预留实现）
    
    后续扩展指南:
    1. pip install python-telegram-bot
    2. 使用 BotFather 创建 Bot 并获取 token，填入 config.json
    3. 实现 _on_message 回调，将消息构造为 IncomingMessage，然后调用 self.agent.handle(msg)
    """

    def __init__(self, config: dict, agent):
        super().__init__('telegram', config, agent)
        self.bot_token = config.get('bot_token', '')

    def start(self):
        print(f"⏳ Telegram 通道暂未实现，跳过启动")

    def stop(self):
        pass

    def send_response(self, message_id: str, response) -> bool:
        pass

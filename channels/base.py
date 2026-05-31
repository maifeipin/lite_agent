from abc import ABC, abstractmethod

class BaseChannel(ABC):
    """IM 通道基类 - 所有平台通道都继承此类"""
    
    def __init__(self, name: str, config: dict, agent):
        self.name = name      # 'feishu', 'telegram', etc.
        self.config = config   # 通道特定配置
        self.agent = agent     # Agent 实例，用于处理消息
    
    @abstractmethod
    def start(self):
        """启动通道（阻塞或非阻塞取决于实现）"""
        pass
    
    @abstractmethod
    def stop(self):
        """停止通道"""
        pass
    
    @abstractmethod
    def send_response(self, message_id: str, response) -> bool:
        """发送回复到平台"""
        pass
    
    def format_card(self, title: str, content: str, color: str = 'blue') -> str:
        """将回复格式化为平台特定的卡片格式（子类可覆盖）"""
        return content

    def broadcast(self, response) -> bool:
        """主动广播消息给当前通道所有历史活跃用户（或指定群组）"""
        return False

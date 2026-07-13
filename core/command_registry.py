"""
指令注册表 — 统一 slash 指令的路由分发。
支持装饰器注册 + 动态加载，解决 agent.py if/elif 链膨胀问题。
"""

from typing import Callable, Dict, Optional, Any

class CommandRegistry:
    """单例指令注册表。技能模块通过 @slash_command 或手动 register() 注册。"""
    _instance: Optional['CommandRegistry'] = None

    def __new__(cls) -> 'CommandRegistry':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._commands: Dict[str, dict] = {}
        return cls._instance

    def register(self, name: str, handler: Callable, *,
                 category: str = '通用',
                 description: str = '',
                 show_in_dashboard: bool = False,
                 admin_only: bool = False,
                 guest_ok: bool = True):
        """注册一个 slash 指令。
        handler 签名: handler(agent, msg, args) -> str | AgentResponse
        admin_only / guest_ok=False 均表示需要管理员权限。
        """
        self._commands[name] = {
            'handler': handler,
            'category': category,
            'description': description,
            'show_in_dashboard': show_in_dashboard,
            'admin_only': admin_only,
            'guest_ok': guest_ok,
        }

    def get(self, name: str) -> Optional[dict]:
        return self._commands.get(name)

    def check_permission(self, cmd: str, is_guest: bool) -> Optional[str]:
        """权限检查。返回错误消息字符串表示拒绝，返回 None 表示放行。"""
        entry = self._commands.get(cmd)
        if not entry:
            return None  # 未注册的不在此处拦截
        if is_guest and (entry.get('admin_only') or not entry.get('guest_ok', True)):
            return "❌ 权限不足：只有管理员可使用该指令"
        return None

    def dispatch(self, cmd: str, agent: Any, msg: Any, args: list) -> Optional[Any]:
        """根据 cmd 名称分发到注册的 handler。未注册返回 None。"""
        entry = self._commands.get(cmd)
        if entry:
            return entry['handler'](agent, msg, args)
        return None

    @property
    def commands(self) -> Dict[str, dict]:
        return dict(self._commands)

    def items_for_dashboard(self) -> list:
        """返回需要在仪表盘展示的指令列表。"""
        return [
            {'name': name, 'description': info['description']}
            for name, info in self._commands.items()
            if info.get('show_in_dashboard')
        ]


# 全局单例
_registry = CommandRegistry()


def slash_command(name: str = '', *, category: str = '通用',
                  description: str = '', show_in_dashboard: bool = False,
                  admin_only: bool = False, guest_ok: bool = True):
    """装饰器: 将函数注册为 slash 指令。
    装饰的函数签名为: func(agent, msg, args) -> str | AgentResponse
    """
    def decorator(func: Callable):
        cmd_name = name or '/' + func.__name__.lstrip('_')
        _registry.register(cmd_name, func, category=category,
                           description=description,
                           show_in_dashboard=show_in_dashboard,
                           admin_only=admin_only, guest_ok=guest_ok)
        return func
    return decorator


def dispatch(cmd: str, agent: Any, msg: Any, args: list) -> Optional[Any]:
    """快捷分发入口。"""
    return _registry.dispatch(cmd, agent, msg, args)

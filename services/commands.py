"""
系统与通用 Slash 指令服务 (services.commands)。
支持多实例隔离、装饰器注册与动态注销，解决 agent.py if/elif 链膨胀与全局状态污染问题。
"""

from typing import Callable, Dict, Optional, Any, List


class CommandRegistry:
    """指令注册表。支持独立实例化实现宿主隔离，支持注销释放。"""

    def __init__(self):
        self._commands: Dict[str, dict] = {}

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

    def unregister(self, name: str) -> bool:
        """注销指定指令。用于插件卸载或测试隔离。"""
        if name in self._commands:
            del self._commands[name]
            return True
        return False

    def clear(self):
        """清空所有指令。"""
        self._commands.clear()

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

    def items_for_dashboard(self) -> List[Dict[str, str]]:
        """返回需要在仪表盘展示的指令列表。"""
        return [
            {'name': name, 'description': info['description']}
            for name, info in self._commands.items()
            if info.get('show_in_dashboard')
        ]


# 默认全局实例 (保留单例向后兼容)
default_command_registry = CommandRegistry()


def slash_command(name: str = '', *, category: str = '通用',
                  description: str = '', show_in_dashboard: bool = False,
                  admin_only: bool = False, guest_ok: bool = True,
                  registry: Optional[CommandRegistry] = None):
    """装饰器: 将函数注册为 slash 指令。
    可指定 registry 实例；若未指定则注册到默认全局注册表。
    """
    target_registry = registry or default_command_registry

    def decorator(func: Callable):
        cmd_name = name or '/' + func.__name__.lstrip('_')
        target_registry.register(
            cmd_name, func, category=category,
            description=description,
            show_in_dashboard=show_in_dashboard,
            admin_only=admin_only, guest_ok=guest_ok
        )
        return func
    return decorator


def dispatch(cmd: str, agent: Any, msg: Any, args: list) -> Optional[Any]:
    """默认全局快捷分发入口。"""
    return default_command_registry.dispatch(cmd, agent, msg, args)

"""
通用工作流注册表服务 (services.workflows)。
用于解耦内核任务编排器与具体业务确定性流程 (如 BillingWorkflow)。
"""

from typing import Callable, Dict, Optional, Any, List


class WorkflowRegistry:
    """工作流注册表。支持实例隔离、重名检查与动态注销。"""

    def __init__(self):
        self._workflows: Dict[str, dict] = {}

    def register(
        self,
        name: str,
        factory: Callable[..., Any],
        matcher: Callable[[dict, str], bool],
        *,
        description: str = "",
    ) -> None:
        """注册一个确定性工作流。
        name: 工作流唯一标识
        factory: 工厂方法 (config, router, ledger) -> workflow_instance
        matcher: 匹配方法 (config, text) -> bool
        """
        if name in self._workflows:
            raise ValueError(f"工作流已存在，禁止重复注册: {name}")
        self._workflows[name] = {
            "factory": factory,
            "matcher": matcher,
            "description": description,
        }

    def unregister(self, name: str) -> bool:
        """注销指定工作流。"""
        if name in self._workflows:
            del self._workflows[name]
            return True
        return False

    def clear(self) -> None:
        """清空所有注册的工作流。"""
        self._workflows.clear()

    def get(self, name: str) -> Optional[dict]:
        return self._workflows.get(name)

    def match(self, config: dict, text: str) -> Optional[str]:
        """按注册顺序检查是否有工作流匹配当前请求。返回匹配到的工作流名称，未匹配返回 None。"""
        for name, entry in self._workflows.items():
            try:
                if entry["matcher"](config, text):
                    return name
            except Exception:
                continue
        return None

    def create(self, name: str, config: dict, router: Any = None, ledger: Any = None) -> Optional[Any]:
        """实例化指定工作流。未注册返回 None。"""
        entry = self._workflows.get(name)
        if entry:
            return entry["factory"](config, router, ledger)
        return None

    def list_workflows(self) -> List[str]:
        return list(self._workflows.keys())


default_workflow_registry = WorkflowRegistry()

"""
通用 HTTP 业务路由注册表 (services.routes)。
支持路径前缀匹配、精确匹配、方法过滤与动态注销，供业务插件登记专属端点。
"""

from typing import Callable, Dict, Optional, Any, List, Tuple


class RouteRegistry:
    """路由注册表。支持实例隔离、重名检查与动态注销。"""

    def __init__(self):
        self._routes: List[Dict[str, Any]] = []

    def register(
        self,
        method: str,
        pattern: str,
        handler: Callable[..., Any],
        *,
        is_prefix: bool = False,
        auth_required: bool = True,
        cors_restricted: bool = False,
        description: str = "",
    ) -> None:
        """注册一个 HTTP 路由。
        method: 'GET', 'POST', 'PUT', 'DELETE', 'ALL'
        pattern: 路径，如 '/api/v1/todos'
        """
        method_upper = method.upper()
        for r in self._routes:
            if r["method"] == method_upper and r["pattern"] == pattern:
                raise ValueError(f"路由已存在，禁止重复注册: {method_upper} {pattern}")

        self._routes.append({
            "method": method_upper,
            "pattern": pattern,
            "handler": handler,
            "is_prefix": is_prefix,
            "auth_required": auth_required,
            "cors_restricted": cors_restricted,
            "description": description,
        })

    def unregister(self, method: str, pattern: str) -> bool:
        """注销指定路由。"""
        method_upper = method.upper()
        for i, r in enumerate(self._routes):
            if r["method"] == method_upper and r["pattern"] == pattern:
                self._routes.pop(i)
                return True
        return False

    def clear(self) -> None:
        """清空所有注册的路由。"""
        self._routes.clear()

    def match(self, method: str, path: str) -> Optional[Dict[str, Any]]:
        """按注册顺序匹配路由。未匹配返回 None。"""
        method_upper = method.upper()
        for r in self._routes:
            if r["method"] != "ALL" and r["method"] != method_upper:
                continue
            if r["is_prefix"]:
                if path == r["pattern"] or path.startswith(r["pattern"].rstrip("/") + "/"):
                    return r
            else:
                if path == r["pattern"]:
                    return r
        return None

    def list_routes(self) -> List[Tuple[str, str]]:
        return [(r["method"], r["pattern"]) for r in self._routes]

    def is_cors_restricted(self, path: str) -> bool:
        """检查指定路径是否匹配任意要求 CORS 受限（仅允许受信任 Origin）的路由。"""
        for r in self._routes:
            if not r.get("cors_restricted"):
                continue
            if r["is_prefix"]:
                if path == r["pattern"] or path.startswith(r["pattern"].rstrip("/") + "/"):
                    return True
            else:
                if path == r["pattern"]:
                    return True
        return False


default_route_registry = RouteRegistry()

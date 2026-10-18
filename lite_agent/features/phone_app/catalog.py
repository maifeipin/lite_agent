"""
可信 App 目录与能力注册表 (features.phone_app.catalog).
严格遵循第 5 节与第 15 节要求：
- 严禁浏览器传入任意包名直接执行。
- 联合校验 device_id、app_id、action 及 filter 兼容性。
- 未真机完全验收的动作禁止声明为 validated。
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Set, Tuple


@dataclass
class AppCatalogItem:
    app_id: str
    display_name: str
    package_name: str
    adapter_version: str
    status: str  # validated | experimental | disabled
    supported_actions: List[str]
    experimental_actions: List[str]
    supported_filters: List[str]
    supported_device_ids: List[str]
    min_android_version: int = 10
    evidence_ref: str = ""
    disabled_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# 静态受控目录：实测真机调通的项才标记为 validated
BUILTIN_APP_CATALOG: Dict[str, AppCatalogItem] = {
    "jd": AppCatalogItem(
        app_id="jd",
        display_name="京东",
        package_name="com.jingdong.app.mall",
        adapter_version="1.0.0",
        status="validated",
        supported_actions=[
            "search", "restore_ready", "search_products"
        ],
        experimental_actions=[
            "open_product", "read_product_metrics", "open_shop", "read_shop_info", "read_logistics"
        ],
        supported_filters=["sales", "positive_rate", "shop_types", "logistics"],
        supported_device_ids=["c68a3cf2"],  # OnePlus 9
        min_android_version=10,
        evidence_ref="20261018-jd-oneplus9-benchmark-20",
    ),
    "douyin": AppCatalogItem(
        app_id="douyin",
        display_name="抖音",
        package_name="com.ss.android.ugc.aweme",
        adapter_version="0.1.0",
        status="experimental",
        supported_actions=["search", "restore_ready"],
        experimental_actions=[],
        supported_filters=[],
        supported_device_ids=["c68a3cf2"],
        min_android_version=10,
        disabled_reason="商品与店铺条件细粒度采集尚未真机验收",
    ),
    "bilibili": AppCatalogItem(
        app_id="bilibili",
        display_name="哔哩哔哩",
        package_name="tv.danmaku.bili",
        adapter_version="0.1.0",
        status="disabled",
        supported_actions=[],
        experimental_actions=["search"],
        supported_filters=[],
        supported_device_ids=[],
        disabled_reason="电商筛选能力未开发",
    ),
}


class AppCatalog:
    """可信应用目录管理器。"""

    def __init__(self, custom_catalog: Optional[Dict[str, AppCatalogItem]] = None):
        self._catalog = dict(custom_catalog or BUILTIN_APP_CATALOG)

    def get_app(self, app_id: str) -> Optional[AppCatalogItem]:
        return self._catalog.get(app_id)

    def list_available_apps(
        self,
        device_id: Optional[str] = None,
        action: Optional[str] = None,
        only_validated: bool = True
    ) -> List[AppCatalogItem]:
        res = []
        for item in self._catalog.values():
            if only_validated and item.status != "validated":
                continue
            if device_id and item.supported_device_ids and (device_id not in item.supported_device_ids):
                continue
            if action:
                all_actions = item.supported_actions + (item.experimental_actions if not only_validated else [])
                if action not in all_actions:
                    continue
            res.append(item)
        return res

    def validate_batch_target(
        self,
        app_ids: List[str],
        device_id: str,
        action: str,
        requested_filters: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """提交前联合校验：设备、应用集、功能支持度及筛选条件交集。"""
        if not app_ids:
            return False, "必须至少选择一个 App"

        for aid in app_ids:
            app = self.get_app(aid)
            if not app:
                return False, f"未知的 App 标识: '{aid}'"
            if app.status != "validated":
                return False, f"App '{app.display_name}' 状态为 {app.status}，不可用于正式生产任务 ({app.disabled_reason})"
            if app.supported_device_ids and (device_id not in app.supported_device_ids):
                return False, f"App '{app.display_name}' 尚未在设备 '{device_id}' 上验证通过"
            if action not in app.supported_actions:
                return False, f"App '{app.display_name}' 尚未完全验证所选功能 '{action}'"

            # 校验筛选条件是否被该 App 声明支持 (R04)
            if requested_filters:
                for f_key in requested_filters.keys():
                    if f_key not in app.supported_filters:
                        return False, f"App '{app.display_name}' 尚未支持 '{f_key}' 筛选条件，请调整后再提交"

        return True, None

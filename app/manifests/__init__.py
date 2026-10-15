"""
应用模式与插件启用清单 (纯数据元数据)。
严格禁止在清单中导入任何 features 或 skills 业务模块。
"""

from typing import List, Dict, Any

# Core 基础模式：零业务插件
CORE_MODE_PLUGINS: List[Dict[str, Any]] = []

# Full 完整模式：显式声明的插件清单 (阶段二由 app 显式装配)
FULL_MODE_PLUGINS: List[Dict[str, Any]] = [
    {
        "name": "billing",
        "entry": "features.billing",
        "description": "账单工作流、证据展示与提醒策略",
    },
    {
        "name": "todo",
        "entry": "features.todo",
        "description": "待办生命周期、仓储与周期提醒",
    },
    {
        "name": "review_meeting",
        "entry": "features.review_meeting",
        "description": "评审会议状态、授权与通知",
    },
]

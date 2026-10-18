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
        "entry": "lite_agent.features.billing",
        "description": "账单确定性工作流、报表与提醒",
        "dependencies": [],
    },
    {
        "name": "todo",
        "entry": "lite_agent.features.todo",
        "description": "待办事项全生命周期、仓储与周期提醒",
        "dependencies": [],
    },
    {
        "name": "review_meeting",
        "entry": "lite_agent.features.review_meeting",
        "description": "会审决策系统状态机、授权与出箱",
        "dependencies": [],
    },
    {
        "name": "mail_ops",
        "entry": "lite_agent.features.mail_ops",
        "description": "邮件抓取、LLM摘要打标与检索",
        "dependencies": [],
    },
    {
        "name": "sys_ops",
        "entry": "lite_agent.features.sys_ops",
        "description": "系统健康自检、备份、日志与安全巡检",
        "dependencies": [],
    },
    {
        "name": "network_ops",
        "entry": "lite_agent.features.network_ops",
        "description": "Socks5代理管理与Edge边缘节点网络调度",
        "dependencies": [],
    },
    {
        "name": "content_ops",
        "entry": "lite_agent.features.content_ops",
        "description": "RSS订阅早报、网页剪藏、搜索与知识归档",
        "dependencies": [],
    },
    {
        "name": "phone_app",
        "entry": "lite_agent.features.phone_app",
        "description": "Phone App 执行层、审核工作流与真机调度中枢",
        "dependencies": [],
    },
]

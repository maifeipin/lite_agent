import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.features.sys_ops import health_service


@skill(
    name='ops_self_check',
    description='运行系统全面健康自检，检查 Agent 进程状态、网络、配置与核心依赖。'
)
def ops_self_check() -> str:
    return health_service.ops_self_check()

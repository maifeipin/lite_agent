import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from features.content_ops import content_service


@skill(
    name='ops_rss_node_status',
    description='检查 RSS 节点的更新状态，找出最后一次更新超过指定小时数未更新的异常节点。',
    params={
        'hours_threshold': {
            'type': 'integer',
            'description': '检查断更的时间阈值（小时），默认为 72 小时',
            'default': 72
        }
    }
)
def ops_rss_node_status(hours_threshold: int = 72) -> str:
    return content_service.get_rss_node_status(hours_threshold=hours_threshold)

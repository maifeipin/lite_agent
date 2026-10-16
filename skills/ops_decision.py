import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from features.review_meeting.decision_service import (
    CriteriaScore,
    BaseScores,
    DecisionResult,
    PeerReview,
    get_models_from_config,
    get_task_profile,
    build_decision_brief,
    call_model,
    compute_weighted_score,
    calculate_std_dev,
    run_ops_decision,
)

# 兼容内部命名
_get_models_from_config = get_models_from_config
_get_task_profile = get_task_profile
_build_decision_brief = build_decision_brief
_call_model = call_model
_compute_weighted_score = compute_weighted_score
_calculate_std_dev = calculate_std_dev


@skill(
    name='ops_decision',
    description='多模型评判委员会引擎。用于对复杂报告、需求或商业行动进行多专家并行评分与盲审。',
    params={
        'task_type': {'type': 'string', 'description': '评估的任务类型，如果没有指定请传 default', 'default': 'default'},
        'topic': {'type': 'string', 'description': '需要交给委员会进行评估的议题数据'},
        'session_id': {'type': 'string', 'description': '可选。会话标识，用于把本次表决挂靠到整体会话链路。不传则不绑定会话。', 'default': ''},
        'trace_id': {'type': 'string', 'description': '可选。调用链路标识（如 DAG task_id）。传了则审计归入该 trace 目录，便于聚合同一链路的多次表决。', 'default': ''}
    }
)
def ops_decision(task_type: str, topic: str, session_id: str = '', trace_id: str = '') -> str:
    return run_ops_decision(task_type=task_type, topic=topic, session_id=session_id, trace_id=trace_id)

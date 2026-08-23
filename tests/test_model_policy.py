import pytest

from core.model_policy import (
    ExecutionPolicy,
    ModelLock,
    ModelPolicyError,
    ModelSelector,
)


def _config():
    return {
        "llm": {
            "default": "glm",
            "models": {
                "glm": {"aliases": ["glm default"]},
                "doubao-pro": {"aliases": ["doubao", "豆包"]},
                "flash": {"aliases": ["ds"]},
            },
        },
        "task_routing": {
            "planner_model": "glm",
            "aggregator_model": "glm",
            "route_rules": [{
                "type": "text",
                "model": "glm",
                "fallback": "flash",
                "allowed_models": ["glm", "doubao-pro", "flash"],
            }, {
                "type": "code",
                "model": "glm",
                "fallback": "flash",
                "allowed_models": ["glm", "flash"],
            }],
        },
    }


def test_aliases_resolve_to_one_canonical_model():
    selector = ModelSelector(_config())

    assert selector.resolve_name("doubao") == "doubao-pro"
    assert selector.resolve_name("豆包") == "doubao-pro"
    assert selector.resolve_name("doubao pro") == "doubao-pro"
    assert selector.resolve_name("doubao-pro") == "doubao-pro"


def test_hard_user_lock_wins_for_all_dag_roles():
    selector = ModelSelector(_config())
    policy = ExecutionPolicy(
        requested_model="doubao-pro",
        model_lock=ModelLock.HARD,
        lock_source="user",
    )

    planner = selector.select("planner", policy=policy)
    worker = selector.select("worker", subtask_type="text", policy=policy)
    aggregator = selector.select("aggregator", policy=policy)

    assert planner.model == "doubao-pro"
    assert worker.model == "doubao-pro"
    assert aggregator.model == "doubao-pro"
    assert planner.reason == "user:hard"
    assert worker.fallback_models == ()


def test_hard_user_lock_is_blocked_by_route_allowlist():
    selector = ModelSelector(_config())
    policy = ExecutionPolicy(
        requested_model="doubao-pro",
        model_lock=ModelLock.HARD,
        lock_source="user",
    )

    with pytest.raises(ModelPolicyError, match="不允许模型"):
        selector.select("worker", subtask_type="code", policy=policy)


def test_auto_worker_uses_route_and_declared_fallback():
    decision = ModelSelector(_config()).select(
        "worker", subtask_type="text"
    )

    assert decision.model == "glm"
    assert decision.reason == "route:text"
    assert decision.fallback_models == ("flash",)


def test_preferred_model_beats_route_but_keeps_declared_fallback():
    policy = ExecutionPolicy(
        requested_model="doubao-pro",
        model_lock=ModelLock.PREFERRED,
        lock_source="task_spec",
    )

    decision = ModelSelector(_config()).select(
        "worker", subtask_type="text", policy=policy
    )

    assert decision.model == "doubao-pro"
    assert decision.reason == "task_spec:preferred"
    assert decision.fallback_models == ("flash",)


def test_unknown_explicit_model_is_rejected():
    selector = ModelSelector(_config())

    with pytest.raises(ModelPolicyError, match="未配置模型"):
        selector.resolve_name("not-real", required=True)


def test_disjoint_policy_and_route_allowlists_block_selection():
    selector = ModelSelector(_config())
    policy = ExecutionPolicy(allowed_models=("doubao-pro",))

    with pytest.raises(ModelPolicyError, match="没有可用"):
        selector.select("worker", subtask_type="code", policy=policy)


def test_allowed_model_fallback_preserves_policy_order():
    config = _config()
    config["task_routing"]["route_rules"].append({
        "type": "data_analysis",
        "model": "not-allowed",
        "allowed_models": ["glm", "doubao-pro"],
    })
    selector = ModelSelector(config)
    policy = ExecutionPolicy(allowed_models=("doubao-pro", "glm"))

    decision = selector.select(
        "worker", subtask_type="data_analysis", policy=policy
    )

    assert decision.model == "doubao-pro"

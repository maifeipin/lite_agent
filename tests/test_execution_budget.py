import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.execution_budget import ExecutionBudget
from core.model_policy import ExecutionPolicy
from core.subtask_dag import Subtask, SubtaskDAG, SubtaskStatus
from core.task_orchestrator import TaskOrchestrator


def test_budget_accumulates_planner_worker_and_aggregator_usage():
    budget = ExecutionBudget(max_steps=10, max_tokens=1000)

    budget.consume("planner", steps=1, tokens=100)
    budget.consume("worker", steps=3, tokens=400)
    budget.consume("aggregator", steps=1, tokens=200)

    current = budget.snapshot()
    assert current.used_steps == 5
    assert current.used_tokens == 700
    assert current.remaining_steps == 5
    assert current.remaining_tokens == 300
    assert budget.usage_by_role()["planner"] == {"steps": 1, "tokens": 100}


def test_worker_share_reserves_one_aggregation_step():
    budget = ExecutionBudget(max_steps=10, max_tokens=1000)
    budget.consume("planner", steps=1, tokens=100)

    steps, tokens = budget.worker_share(batch_size=2, reserve_steps=1)

    assert steps == 4
    assert tokens == 450


def test_budget_never_reports_negative_remaining_values():
    budget = ExecutionBudget(max_steps=2, max_tokens=10)

    budget.consume("worker", steps=3, tokens=20)

    current = budget.snapshot()
    assert current.remaining_steps == 0
    assert current.remaining_tokens == 0
    assert budget.can_start() is False


def test_orchestrator_uses_policy_budget_across_all_llm_roles():
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.dag_max_steps = 30
    orch.dag_max_tokens = 200000
    orch.max_parallel = 3
    orch.max_depth = 5
    orch.subtask_timeout = 10
    orch.ledger = None
    orch.session_mgr = MagicMock()
    orch.executor = ThreadPoolExecutor(max_workers=1)
    observed = {}

    def fake_plan(goal, max_steps=None, budget=None, **kwargs):
        observed["plan_max_steps"] = max_steps
        budget.consume("planner", steps=1, tokens=10)
        return [Subtask(id="s1", name="one")], ""

    def fake_route(subtasks, **kwargs):
        subtasks[0].assigned_model = "local"

    def fake_worker(subtask, upstream, results, lock, *args):
        worker_max_steps, worker_tokens = args[-2:]
        observed["worker_limits"] = (worker_max_steps, worker_tokens)
        with lock:
            results[subtask.id] = {
                "result": "done", "tool_results": [], "status": "done",
                "error": "", "steps_used": 2, "token_usage": 30,
            }

    def fake_aggregate(dag, goal, budget=None, **kwargs):
        observed["before_aggregate"] = budget.snapshot()
        budget.consume("aggregator", steps=1, tokens=20)
        return "summary"

    orch._plan = fake_plan
    orch._classify_and_route = fake_route
    orch._run_single_subtask = fake_worker
    orch._aggregate = fake_aggregate

    try:
        result = orch.execute(
            "goal", "session",
            execution_policy=ExecutionPolicy(
                max_steps=7, max_total_tokens=100,
                max_wall_seconds=30, max_parallel_tasks=1,
            ),
        )
    finally:
        orch.executor.shutdown(wait=True)

    assert result == "summary"
    assert observed["plan_max_steps"] == 7
    assert observed["worker_limits"] == (5, 90)
    assert observed["before_aggregate"].used_steps == 3
    assert observed["before_aggregate"].used_tokens == 40


def test_aggregator_does_not_call_model_after_budget_is_exhausted():
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.config = {
        "llm": {"default": "glm", "models": {"glm": {}}},
        "task_routing": {"planner_model": "glm"},
    }
    orch.model_selector = MagicMock()
    orch.model_selector.select.return_value = SimpleNamespace(
        model="glm", reason="test"
    )
    orch.router = MagicMock()
    orch.router.get_call_profile.return_value = {
        "temperature": 0.3, "max_tokens": 4096, "timeout": 60.0,
        "max_retries": 1, "invoke_kwargs": {},
    }
    orch.llm = MagicMock()
    node = Subtask(id="s1", name="one", result="done")
    node.status = SubtaskStatus.DONE
    budget = ExecutionBudget(max_steps=1, max_tokens=100)
    budget.consume("worker", steps=1, tokens=10)

    result = orch._aggregate(SubtaskDAG([node]), "goal", budget=budget)

    assert "总预算已耗尽" in result
    orch.llm.invoke_sync.assert_not_called()

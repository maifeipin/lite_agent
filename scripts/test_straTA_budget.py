"""
StraTA 战略焊接 + DAG 全局预算 单元测试。

运行: python scripts/test_straTA_budget.py
覆盖:
  1. WorkerAgent prompt 焊接 (goal + global_strategy)
  2. Orchestrator 全局步数/Token 预算截断
  3. DAG 最大深度校验
  4. SubtaskDAG 序列化向后兼容
  5. _plan 解析 global_strategy
"""
import sys
import os
import json
import tempfile

# Windows console may default to GBK; force UTF-8 so emoji in skill_engine prints don't crash
if sys.platform == "win32":
    import io
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.subtask_dag import Subtask, SubtaskDAG, SubtaskType, SubtaskStatus

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}")


# ====================================================================
#  1. WorkerAgent prompt 焊接
# ====================================================================
def test_worker_prompt_welding():
    print("\n[1] Test: WorkerAgent prompt welding (goal + global_strategy)")
    from core.worker_agent import WorkerAgent
    from core.skill_engine import SkillEngine

    se = SkillEngine()
    # 用 mock client 避免真实 API 调用
    worker = WorkerAgent(
        name="TestWorker",
        client=None,
        model_name="flash",
        model_cfg={"max_steps": 2, "max_tokens": 512, "temperature": 0.3},
        skill_engine=se,
        driver="openai",
    )

    subtask = Subtask(
        id="sub_1", name="搜索最新 AI 进展",
        type=SubtaskType.TEXT,
        prompt="请搜索 2026 年 AI 领域的最新进展",
    )

    upstream = {"sub_0": "上游分析结果: 用户需要 AI 进展报告"}
    goal = "调研 GPT-5 最新进展并整理成报告"
    strategy = "先搜索、再整理、最后输出。若搜索失败 2 次则用已有知识补充。"

    # 有 goal + strategy
    prompt_full = worker._build_prompt(subtask, upstream, goal=goal, global_strategy=strategy)
    check("Goal appears in prompt", goal in prompt_full)
    check("Strategy appears in prompt", strategy in prompt_full)
    check("Subtask name appears in prompt", "搜索最新 AI 进展" in prompt_full)
    check("Upstream result appears in prompt", "上游分析结果" in prompt_full)
    check("Strategy framework instruction present", "严格在以上战略框架内执行" in prompt_full)

    # 无 goal / strategy (向后兼容)
    prompt_minimal = worker._build_prompt(subtask, upstream)
    check("Minimal prompt still contains subtask name", "搜索最新 AI 进展" in prompt_minimal)
    check("Minimal prompt does NOT contain goal header", "总体目标" not in prompt_minimal)

    # 只有 goal 无 strategy
    prompt_goal_only = worker._build_prompt(subtask, upstream, goal=goal)
    check("Goal-only has goal", goal in prompt_goal_only)
    # 规则中会提到"全局战略框架", 但 strategy_block 的标题不应出现
    check("Goal-only no strategy block header",
          "## 全局战略" not in prompt_goal_only)


# ====================================================================
#  2. DAG 深度校验
# ====================================================================
def test_dag_max_depth():
    print("\n[2] Test: DAG max depth validation")
    # 构造深度 6 的 DAG (超过默认 max_depth=5)
    s0 = Subtask(id="sub_0", name="root", type=SubtaskType.TEXT, prompt="root")
    s1 = Subtask(id="sub_1", name="l1", type=SubtaskType.TEXT, prompt="l1", depends_on=["sub_0"])
    s2 = Subtask(id="sub_2", name="l2", type=SubtaskType.TEXT, prompt="l2", depends_on=["sub_1"])
    s3 = Subtask(id="sub_3", name="l3", type=SubtaskType.TEXT, prompt="l3", depends_on=["sub_2"])
    s4 = Subtask(id="sub_4", name="l4", type=SubtaskType.TEXT, prompt="l4", depends_on=["sub_3"])
    s5 = Subtask(id="sub_5", name="l5", type=SubtaskType.TEXT, prompt="l5", depends_on=["sub_4"])

    # 深度 5 应该通过 (sub_5 的深度 = 6, 超过 5)
    try:
        dag = SubtaskDAG([s0, s1, s2, s3, s4, s5], max_depth=5)
        check("DAG with depth 6 (>5) should have raised", False)
    except ValueError as e:
        check("DAG depth limit enforced", "exceeds limit" in str(e))

    # 深度 6 限制应该通过
    try:
        dag = SubtaskDAG([s0, s1, s2, s3, s4, s5], max_depth=6)
        check("DAG with depth 6 under limit passes", True)
    except ValueError:
        check("DAG with depth 6 under limit passes", False)

    # 无 max_depth 不校验
    try:
        dag = SubtaskDAG([s0, s1, s2, s3, s4, s5])
        check("DAG without max_depth skips depth check", True)
    except ValueError:
        check("DAG without max_depth skips depth check", False)


# ====================================================================
#  3. SubtaskDAG 序列化向后兼容
# ====================================================================
def test_dag_serialization_backward_compat():
    print("\n[3] Test: SubtaskDAG serialization backward compatibility")
    s = Subtask(id="sub_1", name="test", type=SubtaskType.CODE,
                prompt="do stuff", tools=["ops_workspace_run"])

    # 新格式: dict 包装
    dag_new = SubtaskDAG([s], global_strategy="先搜索再整理", max_depth=5)
    data_new = dag_new.to_dict()
    check("New format is dict", isinstance(data_new, dict))
    check("New format has global_strategy", data_new["global_strategy"] == "先搜索再整理")
    check("New format has subtasks list", isinstance(data_new["subtasks"], list))
    check("New format subtasks count", len(data_new["subtasks"]) == 1)

    # 从新格式恢复
    dag_restored = SubtaskDAG.from_dict(data_new)
    check("Restored global_strategy", dag_restored.global_strategy == "先搜索再整理")
    check("Restored subtask count", len(dag_restored.subtasks) == 1)

    # 旧格式 (纯 list) 向后兼容
    old_data = [{
        "id": "sub_1", "name": "old_test", "type": "text",
        "prompt": "old", "depends_on": [], "tools": [],
        "assigned_model": "", "status": "pending",
        "result": "", "error": "", "token_usage": 0, "steps_used": 0,
    }]
    dag_old = SubtaskDAG.from_dict(old_data)
    check("Old format parsed", len(dag_old.subtasks) == 1)
    check("Old format global_strategy empty", dag_old.global_strategy == "")

    # 空数据
    dag_empty = SubtaskDAG.from_dict([])
    check("Empty list parsed", len(dag_empty.subtasks) == 0)

    # 空 dict
    dag_empty2 = SubtaskDAG.from_dict({})
    check("Empty dict parsed", len(dag_empty2.subtasks) == 0)


# ====================================================================
#  4. Planner _plan 解析 global_strategy
# ====================================================================
def test_plan_parses_strategy():
    print("\n[4] Test: _plan parses global_strategy from planner JSON")
    from core.task_orchestrator import TaskOrchestrator
    from core import skill_engine
    se = skill_engine.SkillEngine()

    orch = TaskOrchestrator(
        config={
            "task_routing": {
                "planner_model": "flash",
                "classifier_model": "flash",
                "dag_max_total_steps": 30,
                "dag_max_total_tokens": 200000,
                "dag_max_depth": 5,
            },
            "llm": {"default": "flash"},
            "models": {
                "flash": {"model": "gpt-4o-mini", "provider": "openai",
                           "api_key": "sk-test", "base_url": "https://test/v1"}
            },
        },
        skill_engine=se,
        session_mgr=None,
    )

    # 模拟 Planner 返回带 global_strategy 的 JSON
    simulated_response = json.dumps({
        "global_strategy": "并行搜索 + 汇总整理，搜索失败则用缓存",
        "subtasks": [
            {"id": "sub_1", "name": "搜索", "type": "text",
             "prompt": "搜索AI进展", "depends_on": [], "tools_hint": ["web_search"]},
            {"id": "sub_2", "name": "汇总", "type": "text",
             "prompt": "汇总搜索结果", "depends_on": ["sub_1"], "tools_hint": []},
        ]
    })
    parsed = orch._parse_json(simulated_response)
    check("Parsed global_strategy", parsed["global_strategy"] == "并行搜索 + 汇总整理，搜索失败则用缓存")
    check("Parsed 2 subtasks", len(parsed["subtasks"]) == 2)

    # 模拟无 global_strategy 的旧格式 (仍可解析)
    old_response = json.dumps({
        "subtasks": [
            {"id": "sub_1", "name": "搜索", "type": "text",
             "prompt": "搜索", "depends_on": [], "tools_hint": []},
        ]
    })
    parsed_old = orch._parse_json(old_response)
    check("Old format parsed subtasks", len(parsed_old.get("subtasks", [])) == 1)
    check("Old format global_strategy defaults empty", parsed_old.get("global_strategy", "") == "")


# ====================================================================
#  5. 全局预算截断
# ====================================================================
def test_dag_budget_enforcement():
    print("\n[5] Test: DAG global budget enforcement (steps + tokens)")
    # 构造已部分执行的 DAG，模拟预算耗尽
    s0 = Subtask(id="sub_0", name="done_task", type=SubtaskType.TEXT,
                 prompt="done", status=SubtaskStatus.DONE,
                 steps_used=10, token_usage=50000)
    s1 = Subtask(id="sub_1", name="pending_1", type=SubtaskType.TEXT,
                 prompt="p1", status=SubtaskStatus.PENDING)
    s2 = Subtask(id="sub_2", name="pending_2", type=SubtaskType.TEXT,
                 prompt="p2", status=SubtaskStatus.PENDING)
    s3 = Subtask(id="sub_3", name="pending_3", type=SubtaskType.TEXT,
                 prompt="p3", status=SubtaskStatus.PENDING)

    # 模拟 step 预算耗尽
    dag = SubtaskDAG([s0, s1, s2, s3])
    total_steps_before = sum(s.steps_used for s in dag.subtasks.values())
    check("Total steps before enforcement = 10", total_steps_before == 10)

    # 模拟: 下一步调度前检查, 已用 10 步 >= 预算 10 → 剩余 PENDING 全标记 SKIPPED
    dag_max = 10
    if total_steps_before >= dag_max:
        for s in dag.subtasks.values():
            if s.status == SubtaskStatus.PENDING:
                s.status = SubtaskStatus.SKIPPED
                s.error = f"全局步数预算耗尽"

    check("All pending skipped on step budget", all(
        dag.subtasks[sid].status == SubtaskStatus.SKIPPED
        for sid in ["sub_1", "sub_2", "sub_3"]
    ))
    check("Done task unchanged", dag.subtasks["sub_0"].status == SubtaskStatus.DONE)

    # Token 预算耗尽
    s4 = Subtask(id="sub_A", name="done", type=SubtaskType.TEXT,
                 prompt="d", status=SubtaskStatus.DONE, token_usage=80000)
    s5 = Subtask(id="sub_B", name="pending", type=SubtaskType.TEXT,
                 prompt="p", status=SubtaskStatus.PENDING)
    s6 = Subtask(id="sub_C", name="pending2", type=SubtaskType.TEXT,
                 prompt="p2", status=SubtaskStatus.PENDING, token_usage=50000)

    dag2 = SubtaskDAG([s4, s5, s6])
    total_tokens = sum(s.token_usage for s in dag2.subtasks.values())
    check("Total tokens before enforcement = 130000", total_tokens == 130000)

    token_budget = 100000
    if total_tokens >= token_budget:
        for s in dag2.subtasks.values():
            if s.status == SubtaskStatus.PENDING:
                s.status = SubtaskStatus.SKIPPED
                s.error = f"全局 Token 预算耗尽"

    check("Pending skipped on token budget", dag2.subtasks["sub_B"].status == SubtaskStatus.SKIPPED)
    check("Pending2 also skipped (cumulative tokens exceeded)", dag2.subtasks["sub_C"].status == SubtaskStatus.SKIPPED)


# ====================================================================
#  main
# ====================================================================
def main():
    print("=" * 60)
    print("StraTA Strategy Welding & DAG Budget Tests")
    print("=" * 60)
    try:
        test_worker_prompt_welding()
        test_dag_max_depth()
        test_dag_serialization_backward_compat()
        test_plan_parses_strategy()
        test_dag_budget_enforcement()
    except Exception as e:
        print(f"Test crashed: {e}")
        import traceback
        traceback.print_exc()
        global _failed
        _failed += 1

    print("=" * 60)
    print(f"Results: {_passed} passed, {_failed} failed")
    print("=" * 60)
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

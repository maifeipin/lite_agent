"""
DAG 工具分配修复验证: _classify_and_route 取并集而非覆盖。

验证:
1. route_rule 有 tools + planner tools_hint 有 tools → 并集 (不丢失 planner 的专用工具)
2. route_rule 有 tools + planner tools_hint 空 → 用 route_rule 的
3. route_rule 无 tools + planner 有 → 用 planner 的
4. 两者都空 → 空
5. 并集去重

运行: python scripts/test_dag_tool_alloc.py
不连真实 LLM, 用 mock router 验证 _classify_and_route 的并集逻辑。
"""
import sys
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from subtask_dag import Subtask, SubtaskType

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  pass {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}")


class FakeRouter:
    """模拟 ModelRouter.route: 按 type 返回 (model, client, tool_filter)"""
    def __init__(self, rules):
        self.rules = rules  # {type: (model, tools)}

    def route(self, subtask_type):
        rule = self.rules.get(subtask_type, ("default_model", []))
        return (rule[0], None, rule[1])


def make_subtask(sid, stype, tools_hint):
    return Subtask(id=sid, name=sid, type=stype, prompt="p", depends_on=[], tools=tools_hint)


def run_route(subtasks, router_rules):
    """复刻 _classify_and_route 的并集逻辑 (与 task_orchestrator.py 保持一致)"""
    router = FakeRouter(router_rules)
    for s in subtasks:
        model_name, client, tool_filter = router.route(s.type.value)
        s.assigned_model = model_name
        if tool_filter or s.tools:
            merged = list(tool_filter or [])
            for t in (s.tools or []):
                if t not in merged:
                    merged.append(t)
            s.tools = merged
    return subtasks


def test_union_both_have_tools():
    """1. route_rule + planner 都有 tools → 并集, 不丢 planner 的"""
    print("\n[1] route_rule + planner tools_hint 并集")
    s = make_subtask("sub_4", SubtaskType.CODE, ["web_clip"])
    run_route([s], {"code": ("pro", ["ops_workspace_run"])})
    check("并集含 route_rule 的 ops_workspace_run", "ops_workspace_run" in s.tools)
    check("并集含 planner 的 web_clip (未丢失)", "web_clip" in s.tools)


def test_union_dedup():
    """2. 两者有相同工具 → 去重"""
    print("\n[2] 并集去重")
    s = make_subtask("sub_x", SubtaskType.CODE, ["ops_workspace_run", "web_clip"])
    run_route([s], {"code": ("pro", ["ops_workspace_run"])})
    check("ops_workspace_run 只出现一次", s.tools.count("ops_workspace_run") == 1)


def test_route_rule_only():
    """3. route_rule 有 tools + planner 空 → 用 route_rule 的"""
    print("\n[3] 仅 route_rule 有 tools")
    s = make_subtask("sub_5", SubtaskType.CODE, [])
    run_route([s], {"code": ("pro", ["ops_workspace_run"])})
    check("用 route_rule 的 tools", s.tools == ["ops_workspace_run"])


def test_planner_only():
    """4. route_rule 无 tools + planner 有 → 用 planner 的"""
    print("\n[4] 仅 planner 有 tools")
    s = make_subtask("sub_2", SubtaskType.COMPLEX_REASONING, ["ops_decision"])
    run_route([s], {"complex_reasoning": ("gemini-pro", [])})
    check("用 planner 的 tools (route_rule 空)", s.tools == ["ops_decision"])


def test_both_empty():
    """5. 两者都空 → 空 (worker 用全部工具)"""
    print("\n[5] 两者都空")
    s = make_subtask("sub_1", SubtaskType.TEXT, [])
    run_route([s], {"text": ("gemini-flash", [])})
    check("tools 为空", s.tools == [])


def test_todo_update_gets_tools():
    """6. 模拟真实场景: 更新todo子任务 planner 给 todo_add, code类型 route 给 ops_workspace_run"""
    print("\n[6] 真实场景: 更新todo子任务")
    s = make_subtask("sub_5", SubtaskType.CODE, ["todo_add", "todo_get"])
    run_route([s], {"code": ("pro", ["ops_workspace_run"])})
    check("todo_add 保留 (不再只能写代码摸索)", "todo_add" in s.tools)
    check("todo_get 保留", "todo_get" in s.tools)
    check("ops_workspace_run 也在", "ops_workspace_run" in s.tools)


def main():
    print("=" * 60)
    print("DAG 工具分配修复验证 (并集而非覆盖)")
    print("=" * 60)
    test_union_both_have_tools()
    test_union_dedup()
    test_route_rule_only()
    test_planner_only()
    test_both_empty()
    test_todo_update_gets_tools()
    print("\n" + "=" * 60)
    print(f"result: {_passed} passed, {_failed} failed")
    print("=" * 60)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()

"""
committee 审计 trace 绑定验证。

mock _call_model 避开真实 LLM, 验证:
1. 传 trace_id + session_id → 三层目录 data/committee/{task_type}/{trace_id}/{run_id}/audit.json
   + audit.json 含 trace_id/session_id/created_at 字段
2. 不传 trace_id → 归入 no_trace 目录
3. 不同 trace_id 的表决互不混淆 (各自独立子目录)
4. run_decision.py 旧式调用 (只传 task_type+topic) 仍兼容

运行: python scripts/test_committee_audit_trace.py
"""
import sys
import os
import json
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import skills.ops_decision as ods

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


# 符合 DecisionResult schema 的固定 JSON (mock _call_model 返回它)
MOCK_RESULT_JSON = json.dumps({
    "decision_type": "值得执行",
    "base_scores": {"profitability": 80, "execution": 90, "timeliness": 70},
    "extra_scores": [],
    "model_reported_overall_score": 80,
    "confidence_score": 85,
    "reasoning": "测试理由",
    "evidence_gaps": [],
    "action_item": "测试行动项"
}, ensure_ascii=False)


def setup_mock(project_root):
    """mock _call_model 返回固定 JSON, 并把 project_root 指向临时目录"""
    tmpdir = tempfile.mkdtemp(prefix="committee_test_")

    # mock _call_model
    orig_call = ods._call_model
    def fake_call(router, model_name, prompt, schema_cls):
        return MOCK_RESULT_JSON
    ods._call_model = fake_call

    # mock load_config 返回临时 project_root (审计写到临时目录)
    orig_load = ods.load_config
    def fake_load():
        return {"project_root": tmpdir, "committee": {"models": ["flash"]}}
    ods.load_config = fake_load

    # _get_models_from_config / _get_task_profile 内部也调 load_config, 会拿到 fake
    return tmpdir, orig_call, orig_load


def teardown(orig_call, orig_load):
    ods._call_model = orig_call
    ods.load_config = orig_load


def test_with_trace_id():
    """1. 传 trace_id + session_id → 三层目录 + 字段"""
    print("\n[1] 传 trace_id + session_id")
    tmpdir, orig_call, orig_load = setup_mock(None)
    try:
        result = ods.ops_decision(
            task_type="default",
            topic="测试议题",
            session_id="dingtalk:user123",
            trace_id="task_1d9f4712"
        )
        # 找生成的 audit.json
        found = []
        for root, dirs, files in os.walk(tmpdir):
            for f in files:
                if f == "audit.json":
                    found.append(os.path.join(root, f))
        check("生成了 audit.json", len(found) == 1)
        if found:
            rel = os.path.relpath(found[0], tmpdir).replace("\\", "/")
            check("路径三层: data/committee/default/task_1d9f4712/{run_id}/audit.json",
                  rel.startswith("data/committee/default/task_1d9f4712/") and rel.endswith("/audit.json"))
            data = json.loads(open(found[0], encoding="utf-8").read())
            check("audit.json 含 trace_id", data.get("trace_id") == "task_1d9f4712")
            check("audit.json 含 session_id", data.get("session_id") == "dingtalk:user123")
            check("audit.json 含 created_at", isinstance(data.get("created_at"), (int, float)))
            check("audit.json 含 run_id", bool(data.get("run_id")))
            check("audit.json 含 results", bool(data.get("results")))
    finally:
        teardown(orig_call, orig_load)


def test_without_trace_id():
    """2. 不传 trace_id → no_trace 目录"""
    print("\n[2] 不传 trace_id (旧式调用)")
    tmpdir, orig_call, orig_load = setup_mock(None)
    try:
        result = ods.ops_decision(task_type="default", topic="测试议题")
        found = []
        for root, dirs, files in os.walk(tmpdir):
            for f in files:
                if f == "audit.json":
                    found.append(os.path.join(root, f))
        check("生成了 audit.json", len(found) == 1)
        if found:
            rel = os.path.relpath(found[0], tmpdir).replace("\\", "/")
            check("路径归入 no_trace: data/committee/default/no_trace/{run_id}/audit.json",
                  rel.startswith("data/committee/default/no_trace/") and rel.endswith("/audit.json"))
            data = json.loads(open(found[0], encoding="utf-8").read())
            check("trace_id 字段为空串", data.get("trace_id") == "")
            check("session_id 字段为空串", data.get("session_id") == "")
    finally:
        teardown(orig_call, orig_load)


def test_different_trace_isolation():
    """3. 不同 trace_id 各自独立子目录"""
    print("\n[3] 不同 trace_id 隔离")
    tmpdir, orig_call, orig_load = setup_mock(None)
    try:
        ods.ops_decision(task_type="default", topic="议题A", trace_id="trace_AAA")
        ods.ops_decision(task_type="default", topic="议题B", trace_id="trace_BBB")
        ods.ops_decision(task_type="default", topic="议题C", trace_id="trace_AAA")  # 同 trace 再来一次
        # 收集所有 audit.json 的路径
        paths = []
        for root, dirs, files in os.walk(tmpdir):
            for f in files:
                if f == "audit.json":
                    paths.append(os.path.relpath(os.path.join(root, f), tmpdir).replace("\\", "/"))
        check("共生成 3 个 audit.json", len(paths) == 3)
        trace_aaa = [p for p in paths if "/trace_AAA/" in p]
        trace_bbb = [p for p in paths if "/trace_BBB/" in p]
        check("trace_AAA 下 2 个 (同 trace 聚合)", len(trace_aaa) == 2)
        check("trace_BBB 下 1 个", len(trace_bbb) == 1)
        # 同 trace 的两个 run_id 不同 (不同子目录)
        run_ids_aaa = [p.split("/trace_AAA/")[1].split("/")[0] for p in trace_aaa]
        check("同 trace 的两次 run_id 不同", len(set(run_ids_aaa)) == 2)
    finally:
        teardown(orig_call, orig_load)


def main():
    print("=" * 60)
    print("committee 审计 trace 绑定验证")
    print("=" * 60)
    test_with_trace_id()
    test_without_trace_id()
    test_different_trace_isolation()
    print("\n" + "=" * 60)
    print(f"result: {_passed} passed, {_failed} failed")
    print("=" * 60)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()

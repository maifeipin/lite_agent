from datetime import datetime, timezone

from core.task_spec import (
    BASE_POLICY,
    TaskSpecStore,
    content_digest,
    new_task_spec,
    next_run_at,
    policy_digest,
    preflight,
)


def _config():
    return {
        "llm": {"models": {"flash": {}, "gemini-pro": {}}},
    }


def test_new_spec_contains_immutable_policy_and_passes_preflight():
    spec = new_task_spec("搜索并总结电动自行车")
    report = preflight(spec, _config())

    assert spec["policy"] == BASE_POLICY
    assert spec["contract"]["policy_digest"] == policy_digest()
    assert spec["contract"]["content_digest"] == content_digest(spec)
    assert report["status"] == "ready"


def test_modified_policy_is_hard_blocker():
    spec = new_task_spec("test")
    spec["policy"]["permission_escalation"] = "allow"

    report = preflight(spec, _config())

    assert report["status"] == "blocked"
    assert "POLICY_MISMATCH" in {x["code"] for x in report["findings"]}


def test_invalid_model_budget_and_secret_are_blocked():
    spec = new_task_spec("test")
    spec["execution"]["model_policy"]["preferred_model"] = "missing"
    spec["execution"]["budget"]["max_steps"] = 0
    spec["task"]["context"] = "api_key=1234567890abcdef"

    report = preflight(spec, _config())
    codes = {x["code"] for x in report["findings"]}

    assert {"MODEL_UNAVAILABLE", "BUDGET_INVALID", "SECRET_IN_DOCUMENT"} <= codes


def test_invalid_output_policy_is_blocked():
    spec = new_task_spec("test")
    spec["output"]["full_delivery"] = "webhook"
    spec["output"]["reply_mode"] = "verbose"

    report = preflight(spec, _config())

    assert report["status"] == "blocked"
    assert "INVALID_OUTPUT" in {x["code"] for x in report["findings"]}


def test_public_hedgedoc_requires_explicit_confirmation():
    spec = new_task_spec("publish report")
    spec["output"]["full_delivery"] = "hedgedoc"

    blocked = preflight(spec, _config())
    assert "EXTERNAL_PUBLISH_CONFIRM" in {x["code"] for x in blocked["findings"]}

    spec["execution"]["approval"]["confirmed"] = True
    assert preflight(spec, _config())["status"] == "ready"


def test_missing_required_capability_is_blocked():
    spec = new_task_spec("search")
    spec["execution"]["capabilities"] = [
        {"name": "web.search", "required": True},
        {"name": "optional.pdf", "required": False},
    ]

    report = preflight(spec, _config(), available_capabilities={"other"})

    missing = [x for x in report["findings"] if x["code"] == "CAPABILITY_MISSING"]
    assert len(missing) == 1
    assert "web.search" in missing[0]["message"]


def test_missing_required_input_and_conflicting_network_rules_are_blocked():
    spec = new_task_spec("research")
    spec["task"]["required_inputs"] = {
        "城市": {"description": "目标城市", "required": True, "value": ""}
    }
    spec["execution"]["network"] = {
        "mode": "forbidden", "citation_required": True, "minimum_sources": 3,
    }

    report = preflight(spec, _config())
    codes = {item["code"] for item in report["findings"]}

    assert "MISSING_INPUT" in codes
    assert "INVALID_VALUE" in codes


def test_once_and_repeat_schedule_next_run():
    once = new_task_spec("once")
    once["execution"]["schedule"] = {
        "mode": "once", "run_at": "2026-08-24T08:00:00+08:00",
        "cron": "", "timezone": "Asia/Shanghai",
    }
    assert next_run_at(once) == "2026-08-24T00:00:00+00:00"

    repeat = new_task_spec("repeat")
    repeat["execution"]["schedule"] = {
        "mode": "repeat", "run_at": "", "cron": "*/15 * * * *",
        "timezone": "Asia/Shanghai",
    }
    after = datetime(2026, 8, 23, 0, 7, tzinfo=timezone.utc)
    assert next_run_at(repeat, after) == "2026-08-23T00:15:00+00:00"


def test_store_round_trip_due_and_once_claim(tmp_path):
    store = TaskSpecStore(str(tmp_path / "tasks.db"))
    spec = new_task_spec("scheduled", task_id="abc123")
    spec["execution"]["schedule"] = {
        "mode": "once", "run_at": "2026-08-23T08:00:00+00:00",
        "cron": "", "timezone": "UTC",
    }

    saved = store.save(spec, status="approved", enabled=True)
    assert saved["id"] == "abc123"
    assert len(store.list()) == 1
    assert len(store.due(datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc))) == 1

    store.mark_started("abc123")
    claimed = store.get("abc123")
    assert claimed["enabled"] is False
    assert claimed["last_run_status"] == "running"

    store.mark_finished("abc123", True, "done")
    assert store.get("abc123")["last_run_status"] == "succeeded"


def test_plan_rejects_unknown_tool_dependency_cycle_and_model():
    spec = new_task_spec("planned")
    spec["execution"]["plan"] = [
        {
            "id": "a", "objective": "a", "mode": "tool",
            "depends_on": ["b"], "tool": {"name": "missing_tool", "arguments": {}},
            "executor": {},
        },
        {
            "id": "b", "objective": "b", "mode": "agent",
            "depends_on": ["a"], "executor": {"preferred_model": "missing_model"},
        },
    ]

    report = preflight(spec, _config(), available_tools={"known_tool"})
    codes = [item["code"] for item in report["findings"]]

    assert "INVALID_PLAN" in codes
    assert "CAPABILITY_MISSING" in codes
    assert "MODEL_UNAVAILABLE" in codes

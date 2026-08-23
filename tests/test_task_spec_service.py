import copy
import json
from unittest.mock import MagicMock, patch

import pytest

from core.task_spec import TaskSpecStore, content_digest
from core.task_spec_service import TaskSpecService


def _config():
    return {
        "llm": {
            "default": "pro",
            "models": {"pro": {}, "flash": {}},
        },
        "task_routing": {"planner_model": "pro"},
        "task_specs": {
            "author_model": "pro",
            "validator_model": "pro",
            "capability_map": {"web.search": ["web_search"]},
        },
    }


def _service(tmp_path, model_output):
    router = MagicMock()
    router.models_cfg = {"pro": {}, "flash": {}}
    invoker = MagicMock()
    invoker.invoke_sync.return_value = {
        "content": json.dumps(model_output, ensure_ascii=False),
        "usage_total": 10,
        "finish_reason": "stop",
    }
    router.get_invoker.return_value = invoker
    with patch("core.task_spec_service.ModelRouter", return_value=router):
        service = TaskSpecService(
            _config(), store=TaskSpecStore(str(tmp_path / "specs.db"))
        )
    return service, invoker


def test_generated_unchanged_spec_can_be_confirmed_without_second_review(tmp_path):
    generated = {
        "task": {
            "name": "产品调研", "objective": "搜索产品", "context": "",
            "assumptions": [], "required_inputs": {}, "constraints": [],
            "acceptance_criteria": ["返回来源"],
        },
        "execution": {
            "complexity": "standard",
            "model_policy": {
                "recommended_tier": "low", "preferred_model": "flash",
                "allowed_models": ["flash"], "user_locked": False,
                "cost_advice": "inform",
            },
            "network": {"mode": "required", "citation_required": True,
                        "minimum_sources": 3},
            "capabilities": ["web.search"],
            "budget": {"max_total_tokens": 50000, "max_steps": 10,
                       "max_wall_seconds": 600, "max_parallel_tasks": 3},
            "plan": [],
            "schedule": {"mode": "manual", "run_at": "", "cron": "",
                         "timezone": "Asia/Shanghai"},
        },
    }
    service, invoker = _service(tmp_path, generated)

    created = service.generate("搜索产品")
    result = service.confirm_generated(created["id"])

    assert created["status"] == "draft"
    assert result["status"] == "approved"
    assert invoker.invoke_sync.call_count == 1


def test_author_timeout_creates_editable_fallback_instead_of_failing(tmp_path):
    service, invoker = _service(tmp_path, {})
    invoker.invoke_sync.side_effect = TimeoutError("author timed out")

    created = service.generate("查看一下最近的外币账单")

    assert created["status"] == "review_required"
    assert created["generation"]["status"] == "fallback"
    assert created["generation"]["code"] == "AUTHOR_MODEL_TIMEOUT"
    assert created["spec"]["task"]["objective"] == "查看一下最近的外币账单"
    assert created["spec"]["validation"]["findings"][0]["severity"] == "warning"
    assert service.store.get(created["id"]) is not None


def test_edit_requires_review_and_validator_findings_are_overrideable(tmp_path):
    service, invoker = _service(tmp_path, {
        "passed": False,
        "findings": [{
            "code": "MODEL_COST", "severity": "blocker",
            "message": "建议使用更低成本模型",
        }],
    })
    manual = service.create_manual("清理空间")
    edited = copy.deepcopy(manual["spec"])
    edited["task"]["context"] = "只清理缓存"
    updated = service.update(manual["id"], edited)

    report = service.review(updated["id"])

    assert updated["status"] == "review_required"
    assert report["status"] == "needs_ack"
    assert report["findings"][0]["severity"] == "needs_ack"
    assert report["findings"][0]["overrideable"] is True
    persisted = service.store.get(updated["id"])
    assert persisted["spec"]["validation"]["findings"][0]["code"] == "MODEL_COST"

    approved = service.acknowledge(updated["id"], "我了解现场情况")
    assert approved["status"] == "approved"
    assert approved["spec"]["contract"]["validated_digest"] == content_digest(approved["spec"])
    assert approved["spec"]["validation"]["status"] == "approved_by_user"


def test_hard_preflight_prevents_model_review(tmp_path):
    service, invoker = _service(tmp_path, {"passed": True, "findings": []})
    manual = service.create_manual("test")
    edited = copy.deepcopy(manual["spec"])
    edited["execution"]["model_policy"]["preferred_model"] = "not-configured"
    updated = service.update(manual["id"], edited)

    report = service.review(updated["id"])

    assert report["status"] == "blocked"
    assert "MODEL_UNAVAILABLE" in {
        item["code"] for item in report["preflight"]["findings"]
    }
    invoker.invoke_sync.assert_not_called()
    persisted = service.store.get(updated["id"])
    assert persisted["status"] == "blocked"
    assert persisted["spec"]["validation"]["preflight"]["status"] == "blocked"


def test_import_assigns_new_identity_and_requires_canonical_policy(tmp_path):
    service, _ = _service(tmp_path, {"passed": True, "findings": []})
    original = service.create_manual("uploaded goal")["spec"]
    old_id = original["contract"]["task_id"]

    imported = service.import_spec(original)

    assert imported["id"] != old_id
    assert imported["spec"]["task"]["objective"] == "uploaded goal"
    assert imported["status"] == "review_required"

    original["policy"]["permission_escalation"] = "allow"
    with pytest.raises(ValueError, match="不可变策略"):
        service.import_spec(original)


def test_build_subtasks_uses_capability_map_and_low_cost_tier(tmp_path):
    service, _ = _service(tmp_path, {"passed": True, "findings": []})
    service.config["task_specs"]["model_tiers"] = {"low": ["flash"]}
    manual = service.create_manual("search")
    spec = manual["spec"]
    spec["execution"]["plan"] = [{
        "id": "search_1",
        "objective": "搜索候选",
        "type": "data_analysis",
        "depends_on": [],
        "mode": "tool",
        "capabilities": ["web.search"],
        "executor": {"model_tier": "low"},
        "tool": {"name": "web_search", "arguments": {"query": "候选"}},
    }]

    subtasks = service.build_subtasks(spec)

    assert len(subtasks) == 1
    assert subtasks[0].assigned_model == "flash"
    assert subtasks[0].tools == ["web_search"]
    assert subtasks[0].execution_mode == "tool"

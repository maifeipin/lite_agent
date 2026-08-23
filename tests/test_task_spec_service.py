import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.task_spec import TaskSpecStore, content_digest
from core.task_spec_service import TaskSpecRevisionConflict, TaskSpecService


def _config():
    return {
        "llm": {
            "default": "pro",
            "models": {"pro": {}, "flash": {}},
        },
        "task_routing": {"planner_model": "pro", "route_rules": [
            {"type": "text", "model": "pro", "fallback": "flash"},
        ]},
        "task_specs": {
            "author_model": "pro",
            "validator_model": "pro",
            "capability_map": {"web.search": ["web_search"]},
        },
    }


def _service(tmp_path, model_output, skill_engine=None, request_selector=None,
             config=None):
    effective_config = config or _config()
    router = MagicMock()
    router.models_cfg = copy.deepcopy(effective_config["llm"]["models"])
    router.get_call_profile.return_value = {
        "temperature": 0.3,
        "max_tokens": 8192,
        "timeout": 60.0,
        "max_retries": 0,
        "invoke_kwargs": {},
    }
    invoker = MagicMock()
    invoker.invoke_sync.return_value = {
        "content": json.dumps(model_output, ensure_ascii=False),
        "usage_total": 10,
        "finish_reason": "stop",
    }
    router.get_invoker.return_value = invoker
    with patch("core.task_spec_service.ModelRouter", return_value=router):
        service = TaskSpecService(
            effective_config, skill_engine=skill_engine,
            store=TaskSpecStore(str(tmp_path / "specs.db")),
            request_selector=request_selector,
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


def test_enrich_updates_existing_task_and_increments_revision(tmp_path):
    service, invoker = _service(tmp_path, {
        "task": {"context": "读取已配置的账单数据源"},
        "execution": {
            "complexity": "standard",
            "model_policy": {"preferred_model": "flash"},
        },
    })
    manual = service.create_manual("查看一下最近的外币账单")

    enriched = service.enrich(manual["id"])

    assert enriched["id"] == manual["id"]
    assert enriched["generation"]["status"] == "completed"
    assert enriched["spec"]["contract"]["revision"] == 2
    assert enriched["preflight"]["revision"] == 2
    assert enriched["spec"]["validation"]["preflight"]["revision"] == 2
    assert enriched["spec"]["contract"]["generated_by"] == "pro"
    assert enriched["spec"]["task"]["context"] == "读取已配置的账单数据源"
    assert enriched["spec"]["execution"]["model_policy"]["preferred_model"] == "flash"
    assert invoker.invoke_sync.call_count == 1


def test_enrich_does_not_override_user_locked_runtime_model(tmp_path):
    service, _ = _service(tmp_path, {
        "execution": {
            "model_policy": {
                "preferred_model": "flash",
                "allowed_models": ["flash"],
                "user_locked": False,
            },
        },
    })
    manual = service.create_manual("查看一下最近的外币账单")
    spec = manual["spec"]
    spec["execution"]["model_policy"].update({
        "preferred_model": "pro",
        "allowed_models": ["pro"],
        "user_locked": True,
    })
    service.store.save(spec, status=manual["status"], enabled=False)

    enriched = service.enrich(manual["id"], model="flash")

    policy = enriched["spec"]["execution"]["model_policy"]
    assert policy["preferred_model"] == "pro"
    assert policy["allowed_models"] == ["pro"]
    assert policy["user_locked"] is True
    assert enriched["spec"]["contract"]["generated_by"] == "flash"


def test_author_receives_selector_tool_schemas_and_disables_hidden_retries(tmp_path):
    skill_engine = MagicMock()
    skill_engine.get_all_names.return_value = {"billing_recent", "billing_report"}
    skill_engine.get_schemas_by_names.return_value = [{
        "type": "function",
        "function": {
            "name": "billing_report",
            "description": "生成近期账单报告，包含外币交易",
            "parameters": {
                "type": "object",
                "properties": {"months": {"type": "integer", "default": 3}},
            },
        },
    }]
    selector = MagicMock()
    selector.select.return_value = SimpleNamespace(names=["billing_report"])
    service, invoker = _service(
        tmp_path,
        {"task": {"required_inputs": {}}},
        skill_engine=skill_engine,
        request_selector=selector,
    )

    service.generate("查看一下最近的外币账单")

    selector.select.assert_called_once()
    prompt = invoker.invoke_sync.call_args.kwargs["messages"][0]["content"]
    assert "billing_report" in prompt
    assert '"default": 3' in prompt
    assert invoker.invoke_sync.call_args.kwargs["max_retries"] == 0
    assert invoker.invoke_sync.call_args.kwargs["max_tokens"] == 8192
    assert "response_format" not in invoker.invoke_sync.call_args.kwargs
    assert "thinking" not in invoker.invoke_sync.call_args.kwargs


def test_author_call_profile_is_configurable_per_model_and_role(tmp_path):
    config = _config()
    config["llm"]["models"]["pro"] = {
        "max_tokens": 12000,
        "profiles": {
            "structured_json": {
                "max_tokens": 6000,
                "timeout": 45,
                "max_retries": 1,
                "invoke_kwargs": {
                    "response_format": {"type": "json_object"},
                    "thinking": {"type": "disabled"},
                },
            },
        },
    }
    service, invoker = _service(
        tmp_path, {"task": {"required_inputs": {}}}, config=config
    )
    service.router.get_call_profile.return_value = config["llm"]["models"]["pro"]["profiles"]["structured_json"]

    service.generate("查看最近的外币账单")

    kwargs = invoker.invoke_sync.call_args.kwargs
    assert kwargs["max_tokens"] == 6000
    assert kwargs["timeout"] == 45
    assert kwargs["max_retries"] == 1
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["thinking"] == {"type": "disabled"}


def test_task_spec_reuses_generic_structured_json_profile(tmp_path):
    service, invoker = _service(
        tmp_path, {"task": {"required_inputs": {}}}
    )
    service.router.get_call_profile.return_value = {
        "temperature": 0.1,
        "max_tokens": 7000,
        "timeout": 75.0,
        "max_retries": 1,
        "invoke_kwargs": {
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        },
    }

    service.generate("查看最近的外币账单")

    kwargs = invoker.invoke_sync.call_args.kwargs
    service.router.get_call_profile.assert_called_with(
        "pro", "structured_json"
    )
    assert kwargs["max_tokens"] == 7000
    assert kwargs["timeout"] == 75.0
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["thinking"] == {"type": "disabled"}


def test_task_spec_settings_can_override_model_with_dotted_name(tmp_path):
    config = _config()
    config["llm"]["models"] = {
        "glm-5.3": {"max_tokens": 8192},
        "flash": {"max_tokens": 4096},
    }
    config["llm"]["default"] = "glm-5.3"
    config["task_specs"]["author_model"] = "glm-5.3"
    config["llm"]["models"]["glm-5.3"]["profiles"] = {
        "structured_json": {
            "max_tokens": 7000,
            "invoke_kwargs": {
                "response_format": {"type": "json_object"},
            },
        },
    }
    service, invoker = _service(
        tmp_path, {"task": {"required_inputs": {}}}, config=config
    )
    service.router.get_call_profile.return_value = config["llm"]["models"]["glm-5.3"]["profiles"]["structured_json"]

    service.generate("查看最近的外币账单")

    kwargs = invoker.invoke_sync.call_args.kwargs
    assert kwargs["max_tokens"] == 7000
    assert kwargs["response_format"] == {"type": "json_object"}


def test_task_spec_profile_merges_model_default_and_role_overrides(tmp_path):
    config = _config()
    config["llm"]["models"]["pro"] = {
        "max_tokens": 12000,
        "profiles": {
            "structured_json": {
                "max_tokens": 6000,
                "timeout": 45,
                "invoke_kwargs": {
                    "response_format": {"type": "json_object"},
                    "thinking": {"type": "disabled"},
                },
            },
        },
    }
    config["llm"]["models"]["pro"]["profiles"]["structured_json"].update({
        "max_tokens": 7000,
        "max_retries": 1,
        "invoke_kwargs": {
            "response_format": {"type": "json_object"},
            "thinking": {"type": "auto"},
            "temperature_hint": "stable",
        },
    })
    service, invoker = _service(
        tmp_path, {"task": {"required_inputs": {}}}, config=config
    )
    service.router.get_call_profile.return_value = config["llm"]["models"]["pro"]["profiles"]["structured_json"]

    service.generate("查看最近的外币账单")

    kwargs = invoker.invoke_sync.call_args.kwargs
    assert kwargs["max_tokens"] == 7000
    assert kwargs["timeout"] == 45
    assert kwargs["max_retries"] == 1
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["thinking"] == {"type": "auto"}
    assert kwargs["temperature_hint"] == "stable"


def test_author_does_not_inject_all_tools_for_unknown_intent(tmp_path):
    skill_engine = MagicMock()
    selector = MagicMock()
    selector.select.return_value = SimpleNamespace(names=None)
    service, _ = _service(
        tmp_path,
        {"task": {"required_inputs": {}}},
        skill_engine=skill_engine,
        request_selector=selector,
    )

    service.generate("一个尚未映射领域的复杂目标")

    skill_engine.get_schemas_by_names.assert_not_called()


def test_author_uses_configured_fallback_when_primary_json_is_truncated(tmp_path):
    service, primary = _service(tmp_path, {})
    fallback = MagicMock()
    primary.invoke_sync.return_value = {
        "content": '{"task":', "usage_total": 4096,
        "finish_reason": "length",
    }
    fallback.invoke_sync.return_value = {
        "content": json.dumps({"task": {"required_inputs": {}}}),
        "usage_total": 120, "finish_reason": "stop",
    }
    service.router.get_invoker.side_effect = (
        lambda name, **kwargs: primary if name == "pro" else fallback
    )

    generated = service.generate("查看一下最近的外币账单")

    assert generated["spec"]["contract"]["generated_by"] == "flash"
    assert generated["status"] == "draft"
    assert primary.invoke_sync.call_count == 1
    assert fallback.invoke_sync.call_count == 1


def test_enrich_can_remove_inputs_replaced_by_a_direct_tool(tmp_path):
    service, _ = _service(tmp_path, {
        "task": {"required_inputs": {}},
        "execution": {
            "plan": [{
                "id": "step_1",
                "objective": "读取最近的账单并筛选外币交易",
                "type": "data_analysis",
                "depends_on": [],
                "mode": "tool",
                "capabilities": [],
                "executor": {"preferred_model": "", "model_tier": "low"},
                "tool": {"name": "billing_report", "arguments": {"months": 3}},
            }],
        },
    })
    manual = service.create_manual("查看一下最近的外币账单")
    edited = copy.deepcopy(manual["spec"])
    edited["task"]["required_inputs"] = {
        "bill_data": {"description": "账单数据", "required": True, "value": ""},
        "time_range": {"description": "时间范围", "required": True, "value": ""},
    }
    service.update(manual["id"], edited)

    enriched = service.enrich(manual["id"])

    assert enriched["spec"]["task"]["required_inputs"] == {}
    assert enriched["spec"]["execution"]["plan"][0]["tool"]["name"] == "billing_report"


def test_enrich_can_use_one_time_model_without_changing_default(tmp_path):
    service, invoker = _service(tmp_path, {"task": {"context": "一次性模型"}})
    manual = service.create_manual("查看账单")

    enriched = service.enrich(manual["id"], model="flash")

    assert enriched["spec"]["contract"]["generated_by"] == "flash"
    assert service.author_model == "pro"
    assert service.router.get_invoker.call_args.args[0] == "flash"
    assert invoker.invoke_sync.call_count == 1


def test_enrich_rejects_unconfigured_one_time_model(tmp_path):
    service, invoker = _service(tmp_path, {})
    manual = service.create_manual("查看账单")

    with pytest.raises(ValueError, match="not configured"):
        service.enrich(manual["id"], model="unknown-pro")

    invoker.invoke_sync.assert_not_called()


def test_model_input_default_is_suggested_but_requires_user_confirmation(tmp_path):
    service, _ = _service(tmp_path, {
        "task": {
            "required_inputs": {
                "time_range": {
                    "description": "查询时间范围",
                    "required": True,
                    "value": "最近30天",
                }
            }
        }
    })

    generated = service.generate("查看最近的外币账单")
    time_range = generated["spec"]["task"]["required_inputs"]["time_range"]

    assert time_range["value"] == ""
    assert time_range["suggested_value"] == "最近30天"
    assert generated["status"] == "blocked"
    assert "MISSING_INPUT" in {
        item["code"] for item in generated["preflight"]["findings"]
    }


def test_enrich_does_not_overwrite_edit_made_while_model_is_running(tmp_path):
    service, invoker = _service(tmp_path, {
        "task": {"context": "模型生成的背景"},
    })
    manual = service.create_manual("查看账单")

    def edit_before_model_returns(*args, **kwargs):
        latest = service.store.get(manual["id"])
        edited = copy.deepcopy(latest["spec"])
        edited["task"]["context"] = "用户刚刚填写的背景"
        service.update(manual["id"], edited)
        return {
            "content": json.dumps({"task": {"context": "模型生成的背景"}}),
            "usage_total": 10,
            "finish_reason": "stop",
        }

    invoker.invoke_sync.side_effect = edit_before_model_returns

    with pytest.raises(TaskSpecRevisionConflict, match="未覆盖当前版本"):
        service.enrich(manual["id"])

    persisted = service.store.get(manual["id"])
    assert persisted["spec"]["task"]["context"] == "用户刚刚填写的背景"
    assert persisted["spec"]["contract"]["revision"] == 2


def test_enrich_timeout_keeps_existing_task_and_is_retryable(tmp_path):
    service, invoker = _service(tmp_path, {})
    manual = service.create_manual("查看一下最近的外币账单")
    invoker.invoke_sync.side_effect = TimeoutError("author timed out")

    result = service.enrich(manual["id"])

    assert result["id"] == manual["id"]
    assert result["generation"]["status"] == "fallback"
    assert result["generation"]["code"] == "AUTHOR_MODEL_TIMEOUT"
    persisted = service.store.get(manual["id"])
    assert persisted["spec"]["contract"]["revision"] == 1
    assert persisted["spec"]["contract"]["generated_by"] == "user"


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


def test_validator_receives_sanitized_runtime_facts_without_stale_findings(tmp_path):
    config = _config()
    config["output_delivery"] = {
        "email": {
            "enabled": True,
            "recipient": "private-owner@example.com",
        },
    }
    service, invoker = _service(
        tmp_path, {"passed": True, "findings": []}, config=config
    )
    manual = service.create_manual("发送账单报告")
    spec = copy.deepcopy(manual["spec"])
    spec["validation"] = {
        "findings": [{"code": "email_recipient_missing"}],
    }
    service.store.save(spec, status="review_required", enabled=False)

    report = service.review(manual["id"])

    assert report["status"] == "approved"
    prompt = invoker.invoke_sync.call_args.kwargs["messages"][0]["content"]
    assert '"configured": true' in prompt
    assert '"recipient_source": "server_configuration"' in prompt
    assert '"final_aggregator"' in prompt
    assert "private-owner@example.com" not in prompt
    assert "email_recipient_missing" not in prompt


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


def test_validator_timeout_keeps_task_editable_and_retryable(tmp_path):
    service, invoker = _service(tmp_path, {})
    manual = service.create_manual("查看外币账单")
    invoker.invoke_sync.side_effect = TimeoutError("validator timed out")

    report = service.review(manual["id"])

    assert report["status"] == "review_required"
    assert report["findings"][0]["code"] == "VALIDATOR_MODEL_TIMEOUT"
    persisted = service.store.get(manual["id"])
    assert persisted["status"] == "review_required"
    assert persisted["spec"]["validation"]["status"] == "review_required"


def test_validator_uses_configured_fallback_when_primary_json_is_truncated(tmp_path):
    service, primary = _service(tmp_path, {})
    fallback = MagicMock()
    primary.invoke_sync.return_value = {
        "content": '{"passed":', "usage_total": 4096,
        "finish_reason": "length",
    }
    fallback.invoke_sync.return_value = {
        "content": json.dumps({"passed": True, "findings": []}),
        "usage_total": 80, "finish_reason": "stop",
    }
    service.router.get_invoker.side_effect = (
        lambda name, **kwargs: primary if name == "pro" else fallback
    )
    manual = service.create_manual("查看最近的外币账单")

    report = service.review(manual["id"])

    assert report["status"] == "approved"
    assert primary.invoke_sync.call_count == 1
    assert fallback.invoke_sync.call_count == 1


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

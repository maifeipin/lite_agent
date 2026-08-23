from core.model_config import validate_model_config


def _config(models):
    return {"llm": {"models": models, "default": next(iter(models))}}


def _model(model_name, api_key=""):
    return {
        "driver": "ark",
        "base_url": "https://ark.example.test/v3",
        "model": model_name,
        "api_key": api_key,
    }


def test_missing_api_key_for_single_model_keeps_specific_error():
    issues = validate_model_config(_config({"doubao": _model("doubao")}))

    errors = [message for level, message in issues if level == "error"]
    assert errors == ["模型 'doubao' 缺少 api_key（或对应环境变量未配置）"]


def test_missing_shared_api_key_is_reported_once_for_credential_scope():
    issues = validate_model_config(_config({
        "doubao": _model("doubao"),
        "glm": _model("glm"),
    }))

    errors = [message for level, message in issues if level == "error"]
    assert len(errors) == 1
    assert "共享凭据" in errors[0]
    assert "'doubao'" in errors[0]
    assert "'glm'" in errors[0]


def test_task_spec_models_are_validated():
    config = _config({"flash": _model("flash", api_key="x")})
    config["task_specs"] = {
        "author_model": "missing-author",
        "validator_model": "missing-validator",
        "model_tiers": {"low": ["flash", "missing-tier"]},
    }

    errors = [message for level, message in validate_model_config(config) if level == "error"]

    assert any("task_specs.author_model" in message for message in errors)
    assert any("task_specs.validator_model" in message for message in errors)
    assert any("task_specs.model_tiers.low" in message for message in errors)


def test_duplicate_model_alias_is_rejected():
    config = _config({
        "doubao": _model("doubao") | {"aliases": ["fast"]},
        "flash": _model("flash") | {"aliases": ["fast"]},
    })

    errors = [message for level, message in validate_model_config(config)
              if level == "error"]

    assert any("模型别名 'fast' 冲突" in message for message in errors)


def test_native_gemini_can_be_default_and_simple_model():
    config = _config({
        "gemini-flash": {
            "driver": "gemini_native", "model": "gemini-2.5-flash",
            "api_key": "x",
        }
    })
    config["task_routing"] = {"simple_model": "gemini-flash"}

    errors = [message for level, message in validate_model_config(config) if level == "error"]

    assert errors == []


def test_invalid_model_call_profile_is_rejected():
    config = _config({
        "glm": _model("glm", api_key="x") | {
            "profiles": {
                "structured_json": {
                    "max_tokens": 0,
                    "invoke_kwargs": "not-an-object",
                },
            },
        },
    })

    errors = [message for level, message in validate_model_config(config)
              if level == "error"]

    assert any("invoke_kwargs" in message for message in errors)
    assert any("max_tokens" in message for message in errors)

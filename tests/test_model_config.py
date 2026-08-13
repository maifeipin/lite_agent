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

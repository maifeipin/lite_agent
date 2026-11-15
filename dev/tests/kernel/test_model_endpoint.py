import os
from unittest.mock import MagicMock, patch
import pytest

from lite_agent.core.model_config import validate_model_config
from lite_agent.core.model_endpoint import (
    EndpointDefinition,
    ModelDefinition,
    normalize_llm_config,
    resolve_credential,
)
from lite_agent.core.model_router import ModelRouter
from lite_agent.core.llm_error import LLMErrorCategory, classify_llm_error, is_retryable_error


def test_normalize_schema_version_2_native():
    cfg = {
        "schema_version": 2,
        "default": "main-chat",
        "endpoints": {
            "ds-ep": {
                "provider": "deepseek",
                "protocol": "openai_chat_completions",
                "base_url": "https://api.deepseek.com/v1",
                "credential_ref": "DEEPSEEK_API_KEY",
                "management_ref": "deepseek-official-balance"
            }
        },
        "models": {
            "main-chat": {
                "endpoint_ref": "ds-ep",
                "model": "deepseek-v4-flash",
                "aliases": ["ds", "flash"],
                "capabilities": {"sync": True, "stream": True, "tools": True},
                "max_tokens": 4096
            }
        }
    }

    endpoints, models, default_model = normalize_llm_config(cfg)
    assert default_model == "main-chat"
    assert "ds-ep" in endpoints
    assert endpoints["ds-ep"].provider == "deepseek"
    assert endpoints["ds-ep"].credential_ref == "DEEPSEEK_API_KEY"
    assert "main-chat" in models
    assert models["main-chat"].endpoint_ref == "ds-ep"
    assert models["main-chat"].aliases == ["ds", "flash"]


def test_normalize_schema_version_1_legacy_models():
    cfg = {
        "default": "flash",
        "models": {
            "flash": {
                "driver": "deepseek",
                "model": "deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "${DEEPSEEK_API_KEY}"
            }
        }
    }

    endpoints, models, default_model = normalize_llm_config(cfg)
    assert default_model == "flash"
    assert "flash" in models
    ep_id = models["flash"].endpoint_ref
    assert ep_id in endpoints
    assert endpoints[ep_id].provider == "deepseek"
    assert endpoints[ep_id].credential_ref == "DEEPSEEK_API_KEY"


def test_resolve_credential_from_env(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "sk-secret-12345")
    assert resolve_credential("TEST_LLM_KEY") == "sk-secret-12345"
    assert resolve_credential("${TEST_LLM_KEY}") == "sk-secret-12345"
    assert resolve_credential("NONEXISTENT_KEY", "fallback") == "fallback"


def test_validate_model_config_schema_2_valid(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-ok")
    cfg = {
        "llm": {
            "schema_version": 2,
            "default": "flash",
            "endpoints": {
                "ds-ep": {
                    "provider": "deepseek",
                    "protocol": "openai_chat_completions",
                    "base_url": "https://api.deepseek.com/v1",
                    "credential_ref": "DEEPSEEK_API_KEY"
                }
            },
            "models": {
                "flash": {
                    "endpoint_ref": "ds-ep",
                    "model": "deepseek-v4-flash"
                }
            }
        }
    }

    issues = validate_model_config(cfg)
    errors = [msg for level, msg in issues if level == "error"]
    assert len(errors) == 0


def test_validate_model_config_schema_2_missing_endpoint_ref(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-ok")
    cfg = {
        "llm": {
            "schema_version": 2,
            "default": "flash",
            "endpoints": {
                "ds-ep": {
                    "provider": "deepseek",
                    "protocol": "openai_chat_completions",
                    "base_url": "https://api.deepseek.com/v1",
                    "credential_ref": "DEEPSEEK_API_KEY"
                }
            },
            "models": {
                "flash": {
                    "endpoint_ref": "nonexistent-ep",
                    "model": "deepseek-v4-flash"
                }
            }
        }
    }

    issues = validate_model_config(cfg)
    errors = [msg for level, msg in issues if level == "error"]
    assert any("未配置的 endpoint" in err for err in errors)


def test_v1_conflicting_model_fields_rejected_and_binding_unification(monkeypatch):
    """V1: v2 混入旧字段时校验必须报错拒绝，且巡检解析时 endpoint 严格优先于内联残余。"""
    monkeypatch.setenv("REVIEW_FAKE_KEY", "ep-real-key")
    conflicting_cfg = {
        "llm": {
            "schema_version": 2,
            "default": "m",
            "endpoints": {
                "ep": {
                    "provider": "ark",
                    "protocol": "openai_chat_completions",
                    "base_url": "https://ark.example.com/v1",
                    "credential_ref": "REVIEW_FAKE_KEY"
                }
            },
            "models": {
                "m": {
                    "endpoint_ref": "ep",
                    "model": "actual",
                    "driver": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "conflicting-legacy-key"
                }
            }
        }
    }

    # 1. 校验器必须精准报错冲突
    issues = validate_model_config(conflicting_cfg)
    errors = [msg for level, msg in issues if level == "error"]
    assert any("二者冲突" in err for err in errors)

    # 2. 巡检解析绑定严格以 endpoint 为准，绝不被内联 driver/base_url 污染
    from lite_agent.features.sys_ops.llm_ops_service import _resolve_default_model_binding
    def_key, resolved = _resolve_default_model_binding(conflicting_cfg)
    assert def_key == "m"
    assert resolved["driver"] == "ark"
    assert resolved["base_url"] == "https://ark.example.com/v1"
    assert resolved["api_key"] == "ep-real-key"


def test_v2_validation_rejects_missing_env_bad_protocol_and_bad_ref(monkeypatch):
    """V2: 缺失环境变量、非法凭据引用格式、不支持协议、明文 api_key 必须被校验拒绝。"""
    # 1. credential_ref 引用的环境变量不存在
    cfg_missing_env = {
        "llm": {
            "schema_version": 2,
            "default": "m",
            "endpoints": {
                "ep": {
                    "provider": "ark",
                    "protocol": "openai_chat_completions",
                    "base_url": "https://ark.example.com/v1",
                    "credential_ref": "NON_EXISTENT_VAR_ABC123"
                }
            },
            "models": {"m": {"endpoint_ref": "ep", "model": "actual"}}
        }
    }
    issues = validate_model_config(cfg_missing_env)
    assert any("未配置或为空" in msg for lvl, msg in issues if lvl == "error")

    # 2. credential_ref 格式非法（包含空格或非法字符）
    monkeypatch.setenv("OK_KEY", "val")
    cfg_bad_ref = {
        "llm": {
            "schema_version": 2,
            "default": "m",
            "endpoints": {
                "ep": {
                    "provider": "ark",
                    "protocol": "openai_chat_completions",
                    "base_url": "https://ark.example.com/v1",
                    "credential_ref": "invalid env reference!"
                }
            },
            "models": {"m": {"endpoint_ref": "ep", "model": "actual"}}
        }
    }
    issues = validate_model_config(cfg_bad_ref)
    assert any("格式非法" in msg for lvl, msg in issues if lvl == "error")

    # 3. 未知协议
    cfg_bad_proto = {
        "llm": {
            "schema_version": 2,
            "default": "m",
            "endpoints": {
                "ep": {
                    "provider": "ark",
                    "protocol": "unsupported_protocol",
                    "base_url": "https://ark.example.com/v1",
                    "credential_ref": "OK_KEY"
                }
            },
            "models": {"m": {"endpoint_ref": "ep", "model": "actual"}}
        }
    }
    issues = validate_model_config(cfg_bad_proto)
    assert any("不支持的协议" in msg for lvl, msg in issues if lvl == "error")

    # 4. v2 endpoint 禁止直接配明文 api_key
    cfg_plain_key = {
        "llm": {
            "schema_version": 2,
            "default": "m",
            "endpoints": {
                "ep": {
                    "provider": "ark",
                    "protocol": "openai_chat_completions",
                    "base_url": "https://ark.example.com/v1",
                    "credential_ref": "OK_KEY",
                    "api_key": "sk-plain"
                }
            },
            "models": {"m": {"endpoint_ref": "ep", "model": "actual"}}
        }
    }
    issues = validate_model_config(cfg_plain_key)
    assert any("禁止直接配置明文 api_key" in msg for lvl, msg in issues if lvl == "error")


def test_v3_agent_runtime_never_retries_on_auth_or_quota_exhausted(monkeypatch):
    """V3: AgentRuntime 流式遇到 401 Unauthorized 或配额耗尽等不可重试错误时绝不重试。"""
    from lite_agent.core.agent_runtime import AgentRuntime
    from lite_agent.core.model_invoker import ModelEvent, ModelEventType
    from lite_agent.contracts.execution import ExecutionContext

    call_count = 0
    def mock_invoke_stream(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        yield ModelEvent(ModelEventType.ERROR, RuntimeError("HTTP 401 Unauthorized"))

    mock_invoker = MagicMock()
    mock_invoker.invoke_stream.side_effect = mock_invoke_stream
    mock_engine = MagicMock()
    runtime = AgentRuntime(model_invoker=mock_invoker, skill_engine=mock_engine, max_retries=2)

    ctx = ExecutionContext(actor_id="test", max_steps=1)
    events = list(runtime.run([{"role": "user", "content": "hi"}], tools=[], ctx=ctx))

    error_events = [ev for ev in events if ev.type.name == "ERROR"]
    assert len(error_events) == 1
    assert "401 Unauthorized" in str(error_events[0].data.get("msg", ""))
    # 关键验证：即使配置 max_retries=2，因属于 AUTH 错误，调用次数严格为 1，绝不重试！
    assert call_count == 1


def test_v4_capabilities_and_snapshot_unified(monkeypatch):
    """V4: v2 声明的 capabilities.vision=True 必须被 Router 正确识别；快照展示正确。"""
    monkeypatch.setenv("GEMINI_KEY", "fake-gemini-key")
    v2_cfg = {
        "llm": {
            "schema_version": 2,
            "default": "gemini-flash",
            "endpoints": {
                "gemini-ep": {
                    "provider": "gemini_native",
                    "protocol": "gemini_generate_content",
                    "credential_ref": "GEMINI_KEY"
                }
            },
            "models": {
                "gemini-flash": {
                    "endpoint_ref": "gemini-ep",
                    "model": "gemini-2.5-flash",
                    "capabilities": {"vision": True, "stream": False}
                }
            }
        }
    }

    # 1. ModelRouter.supports_vision 返回 True
    with patch("lite_agent.core.model_router.ModelRouter._make_gemini_client"):
        router = ModelRouter(v2_cfg)
        assert router.supports_vision("gemini-flash") is True
        assert router.supports_streaming("gemini-flash") is False

    # 2. ModelSelector._model_supports_streaming 返回 False
    from lite_agent.core.model_policy import ModelSelector
    selector = ModelSelector(v2_cfg)
    assert selector._model_supports_streaming("gemini-flash") is False

    # 3. build_model_snapshot 正确显示 provider 和 configured=True
    from lite_agent.core.model_management import build_model_snapshot
    snapshot = build_model_snapshot(v2_cfg)
    model_entry = next(m for m in snapshot["models"] if m["name"] == "gemini-flash")
    assert model_entry["driver"] == "gemini_native"
    assert model_entry["configured"] is True
    assert "vision" in model_entry["tags"]


def test_classify_llm_error_categories():
    assert classify_llm_error("HTTP 401 Unauthorized: Invalid API key") == LLMErrorCategory.AUTH_401
    assert classify_llm_error("Error: 403 Forbidden Access Denied") == LLMErrorCategory.PERMISSION_403
    assert classify_llm_error("HTTP 429 Too Many Requests: Rate limit exceeded") == LLMErrorCategory.RATE_LIMIT_429
    assert classify_llm_error("Account deactivated due to insufficient_quota") == LLMErrorCategory.QUOTA_EXHAUSTED
    assert classify_llm_error("502 Bad Gateway from upstream proxy") == LLMErrorCategory.SERVER_5XX
    assert classify_llm_error("400 Bad Request: context_length_exceeded") == LLMErrorCategory.BAD_REQUEST_400
    assert classify_llm_error("Response was blocked by content filter") == LLMErrorCategory.CONTENT_FILTER

    assert is_retryable_error(LLMErrorCategory.SERVER_5XX) is True
    assert is_retryable_error(LLMErrorCategory.RATE_LIMIT_429) is True
    assert is_retryable_error(LLMErrorCategory.AUTH_401) is False
    assert is_retryable_error(LLMErrorCategory.BAD_REQUEST_400) is False

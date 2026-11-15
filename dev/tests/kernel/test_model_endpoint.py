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


def test_validate_model_config_schema_2_valid():
    cfg = {
        "llm": {
            "schema_version": 2,
            "default": "flash",
            "endpoints": {
                "ds-ep": {
                    "provider": "deepseek",
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


def test_validate_model_config_schema_2_missing_endpoint_ref():
    cfg = {
        "llm": {
            "schema_version": 2,
            "default": "flash",
            "endpoints": {
                "ds-ep": {
                    "provider": "deepseek",
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

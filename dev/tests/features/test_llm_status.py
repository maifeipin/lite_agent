import json
import urllib.error
from unittest.mock import patch, MagicMock
import io
import pytest

from lite_agent.features.sys_ops.llm_ops_service import (
    check_active_llm_status,
    _is_official_deepseek_endpoint,
    _resolve_default_model_binding,
)


def test_is_official_deepseek_endpoint():
    assert _is_official_deepseek_endpoint("https://api.deepseek.com/v1") is True
    assert _is_official_deepseek_endpoint("https://api.deepseek.com") is True
    assert _is_official_deepseek_endpoint("https://api.deepseek.com/user/balance") is True
    assert _is_official_deepseek_endpoint("https://thirdparty.deepseek.com") is False
    assert _is_official_deepseek_endpoint("https://my-proxy.com/v1") is False
    assert _is_official_deepseek_endpoint("https://ark.cn-beijing.volces.com/api/v3") is False
    assert _is_official_deepseek_endpoint("") is False
    assert _is_official_deepseek_endpoint(None) is False


def test_resolve_default_model_binding_standard():
    config = {
        "llm": {
            "default": "flash",
            "models": {
                "flash": {
                    "driver": "deepseek",
                    "model": "deepseek-v4-flash",
                    "api_key": "sk-model-key",
                    "base_url": "https://api.deepseek.com/v1"
                },
                "doubao": {
                    "driver": "ark",
                    "model": "doubao-pro",
                    "api_key": "ark-model-key"
                }
            }
        }
    }
    key, model_cfg = _resolve_default_model_binding(config)
    assert key == "flash"
    assert model_cfg["api_key"] == "sk-model-key"
    assert model_cfg["model"] == "deepseek-v4-flash"


def test_official_deepseek_balance_success():
    config = {
        "llm": {
            "default": "flash",
            "models": {
                "flash": {
                    "driver": "deepseek",
                    "model": "deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-test-valid-key"
                }
            }
        }
    }

    mock_resp_data = {
        "is_available": True,
        "balance_infos": [
            {"currency": "CNY", "total_balance": "61.96"}
        ]
    }
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(mock_resp_data).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        result = check_active_llm_status(config)
        assert "DeepSeek 账户余额 (官方实时)" in result
        assert "61.96" in result
        assert "CNY" in result
        
        # 验证调用的 URL 与 Authorization Header
        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.deepseek.com/user/balance"
        assert req.get_header("Authorization") == "Bearer sk-test-valid-key"


def test_official_deepseek_401_sanitized():
    config = {
        "llm": {
            "default": "flash",
            "models": {
                "flash": {
                    "driver": "deepseek",
                    "model": "deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-expired-key"
                }
            }
        }
    }

    fp = io.BytesIO(b'{"error":{"message":"Internal sensitive token leak test"}}')
    http_error = urllib.error.HTTPError(
        url="https://api.deepseek.com/user/balance",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=fp
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        result = check_active_llm_status(config)
        assert "认证失败 (HTTP 401)" in result
        assert "DeepSeek 官方端点凭据认证失败" in result
        # 严格防御：不能将 response body 原文透传给上层，防止凭据泄露
        assert "Internal sensitive token leak test" not in result


def test_ark_unsupported_notice_no_network():
    config = {
        "llm": {
            "default": "doubao-pro",
            "models": {
                "doubao-pro": {
                    "driver": "ark",
                    "model": "doubao-seed-2.0-pro",
                    "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
                    "api_key": "ark-39499824-test"
                }
            }
        }
    }

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = check_active_llm_status(config)
        # 绝不发送网络请求到 DeepSeek 或其他服务
        mock_urlopen.assert_not_called()
        assert "火山引擎方舟 (Ark)" in result
        assert "当前厂商/端点未提供公开 API 余额查询" in result
        assert "doubao-pro" in result


def test_gemini_unsupported_notice_no_network():
    config = {
        "llm": {
            "default": "gemini-flash",
            "models": {
                "gemini-flash": {
                    "driver": "gemini_native",
                    "model": "gemini-2.5-flash",
                    "api_key": "AIzaSyFakeKey"
                }
            }
        }
    }

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = check_active_llm_status(config)
        mock_urlopen.assert_not_called()
        assert "Google Gemini" in result
        assert "当前厂商/端点未提供公开 API 余额查询" in result


def test_thirdparty_proxy_not_sent_to_official_deepseek():
    """虽然 driver 标为 deepseek，但 base_url 是第三方中转端点，绝不能把第三方 Key 发去 DeepSeek 官方"""
    config = {
        "llm": {
            "default": "flash",
            "models": {
                "flash": {
                    "driver": "deepseek",
                    "model": "deepseek-v4-flash",
                    "base_url": "https://my-custom-proxy.internal/v1",
                    "api_key": "proxy-secret-key"
                }
            }
        }
    }

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = check_active_llm_status(config)
        mock_urlopen.assert_not_called()
        assert "当前厂商/端点未提供公开 API 余额查询" in result


def test_legacy_top_level_ark_key_mismatch_regression():
    """
    回归测试 VPS1 线上 401 真实事故：
    top-level api_key 写错为 ark-xxxx，但 models.flash 的 api_key 为真实的 sk-deepseek-xxxx。
    必须读取 models.flash 的 Key，决不能读取顶层错误的 ark Key！
    """
    config = {
        "llm": {
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "ark-invalid-legacy-top-key",
            "model": "deepseek-v4-flash",
            "default": "flash",
            "models": {
                "flash": {
                    "driver": "deepseek",
                    "model": "deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-correct-model-key"
                }
            }
        }
    }

    mock_resp_data = {
        "is_available": True,
        "balance_infos": [
            {"currency": "CNY", "total_balance": "100.00"}
        ]
    }
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(mock_resp_data).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        result = check_active_llm_status(config)
        assert "100.00" in result
        req = mock_urlopen.call_args[0][0]
        # 关键断言：发送的必须是 models.flash 的 sk Key，绝不能是顶层的 ark Key！
        assert req.get_header("Authorization") == "Bearer sk-correct-model-key"
        assert "ark-invalid-legacy-top-key" not in req.get_header("Authorization")


def test_unconfigured_or_unresolved_env_key():
    config = {
        "llm": {
            "default": "flash",
            "models": {
                "flash": {
                    "driver": "deepseek",
                    "model": "deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "${UNRESOLVED_ENV_KEY}"
                }
            }
        }
    }

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = check_active_llm_status(config)
        mock_urlopen.assert_not_called()
        assert "未配置或环境变量未解析" in result


def test_config_loader_cleans_legacy_root_keys(tmp_path, monkeypatch):
    """验证当存在 models 字典时，旧的平铺 api_key / base_url 会被彻底过滤，防止复活"""
    import lite_agent.core.config_loader as cl
    monkeypatch.setattr(cl, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(cl, "_DEFAULT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("LITE_AGENT_CONFIG_FILE", raising=False)
    monkeypatch.delenv("LITE_AGENT_CONFIG_ROOT", raising=False)
    cl.bust_base_cache()

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "llm": {
            "api_key": "legacy-root-key",
            "base_url": "https://legacy-root.url",
            "default": "flash",
            "max_tokens": 4096,
            "models": {
                "flash": {
                    "driver": "deepseek",
                    "model": "deepseek-v4-flash",
                    "api_key": "model-specific-key"
                }
            }
        }
    }))

    loaded = cl.load_config()
    llm = loaded["llm"]
    # 顶层凭据被安全剔除
    assert "api_key" not in llm
    assert "base_url" not in llm
    # 非凭据参数保留
    assert llm["max_tokens"] == 4096
    # 模型级定义完好
    assert llm["models"]["flash"]["api_key"] == "model-specific-key"


def test_sqlite_override_protected_keys_rejected(tmp_path, monkeypatch):
    """验证 SQLite 尝试写入 base_url 或复合敏感字段会被严格拦截"""
    import lite_agent.core.config_loader as cl
    monkeypatch.setattr(cl, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(cl, "_db_initialized", False)
    monkeypatch.setattr(cl, "_base_cache", None)

    with pytest.raises(ValueError, match="prohibited"):
        cl.write_setting("llm.models.flash.base_url", "https://malicious.com")

    with pytest.raises(ValueError, match="protected/sensitive"):
        cl.write_setting("llm.models.flash", {"base_url": "https://malicious.com"})

    with pytest.raises(ValueError, match="protected/sensitive"):
        cl.write_setting("llm.models.flash", {"api_key": "stolen-key"})


def test_endpoint_url_strict_validation():
    # 严格检验: 仅允许官方 HTTPS, 443 端口, 无 userinfo, 且 path 白名单
    assert _is_official_deepseek_endpoint("https://api.deepseek.com") is True
    assert _is_official_deepseek_endpoint("https://api.deepseek.com/") is True
    assert _is_official_deepseek_endpoint("https://api.deepseek.com/v1") is True
    assert _is_official_deepseek_endpoint("https://api.deepseek.com/user/balance") is True
    # 拒绝非 HTTPS
    assert _is_official_deepseek_endpoint("http://api.deepseek.com/v1") is False
    # 拒绝含 userinfo
    assert _is_official_deepseek_endpoint("https://user:pass@api.deepseek.com/v1") is False
    # 拒绝自定义端口
    assert _is_official_deepseek_endpoint("https://api.deepseek.com:8443/v1") is False
    # 拒绝非白名单路径
    assert _is_official_deepseek_endpoint("https://api.deepseek.com/v1/unexpected") is False
    assert _is_official_deepseek_endpoint("https://api.deepseek.com/other") is False


def test_legacy_without_explicit_binding_no_network():
    """没有 models 且没有显式 driver 的旧平铺配置，坚决不盲目发送网络请求"""
    config = {
        "llm": {
            "api_key": "fake-legacy-unbound-key"
        }
    }
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = check_active_llm_status(config)
        mock_urlopen.assert_not_called()
        assert "配置错误" in result
        assert "已终止查询" in result


def test_alias_does_not_override_explicit_driver_no_network():
    """别名即使包含 deepseek，只要显式 driver 为 ark，绝不发起 DeepSeek 网络请求"""
    config = {
        "llm": {
            "default": "deepseek-test",
            "models": {
                "deepseek-test": {
                    "driver": "ark",
                    "model": "doubao-pro",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "ark-secret-key"
                }
            }
        }
    }
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = check_active_llm_status(config)
        mock_urlopen.assert_not_called()
        assert "火山引擎方舟" in result
        assert "当前厂商/端点未提供公开 API 余额查询" in result


def test_invalid_default_does_not_query_first_model_no_network():
    """default 拼错或在 models 中不存在时，坚决不静默兜底为第一个模型，直接报错且零网络请求"""
    config = {
        "llm": {
            "default": "typo",
            "models": {
                "first": {
                    "driver": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-real-first-key"
                }
            }
        }
    }
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = check_active_llm_status(config)
        mock_urlopen.assert_not_called()
        assert "配置错误" in result
        assert "已终止查询" in result


def test_response_body_marker_sanitization():
    """当官方接口返回 200 但 is_available=false 且伴随敏感字段时，绝不透传 JSON 原文"""
    config = {
        "llm": {
            "default": "flash",
            "models": {
                "flash": {
                    "driver": "deepseek",
                    "model": "deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-valid-key"
                }
            }
        }
    }
    mock_resp_data = {
        "is_available": False,
        "internal_leak_marker": "SUPER_SECRET_PAYLOAD_12345"
    }
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(mock_resp_data).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = check_active_llm_status(config)
        assert "账户不可用" in result
        assert "SUPER_SECRET_PAYLOAD_12345" not in result


def test_get_model_local_usage_honors_env_session_db_path(tmp_path, monkeypatch):
    """F1: 验证尊重 LITE_AGENT_SESSION_DB_PATH，正确读取独立实例的用量数据"""
    import sqlite3
    import time
    db_file = tmp_path / "configured_sessions.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE api_usage_log (id INTEGER PRIMARY KEY, model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, created_at REAL)"
    )
    conn.execute(
        "INSERT INTO api_usage_log (model, prompt_tokens, completion_tokens, total_tokens, created_at) VALUES (?, ?, ?, ?, ?)",
        ("doubao-seed-2.0-pro", 2345, 10000, 12345, time.time())
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("LITE_AGENT_SESSION_DB_PATH", str(db_file))
    # 将 PROJECT_ROOT 指向空目录，避免读取工作区默认数据库
    import lite_agent.core.constants as consts
    monkeypatch.setattr(consts, "PROJECT_ROOT", str(tmp_path / "empty_dir"))

    config = {
        "llm": {
            "default": "doubao-pro",
            "models": {
                "doubao-pro": {
                    "driver": "ark",
                    "model": "doubao-seed-2.0-pro",
                    "api_key": "ark-test-key"
                }
            }
        }
    }

    result = check_active_llm_status(config)
    assert "12,345" in result
    assert "本地已记录" in result
    assert "暂无调用记录" not in result


def test_get_model_local_usage_db_failure_reports_unavailable(tmp_path, monkeypatch):
    """F2: 验证数据库异常时报告用量统计暂不可用，坚决不伪装成'暂无调用记录'或泄露底层异常"""
    corrupt_file = tmp_path / "corrupt_sessions.db"
    corrupt_file.write_text("NOT A SQLITE FILE")

    monkeypatch.setenv("LITE_AGENT_SESSION_DB_PATH", str(corrupt_file))
    import lite_agent.core.constants as consts
    monkeypatch.setattr(consts, "PROJECT_ROOT", str(tmp_path / "empty_dir"))

    config = {
        "llm": {
            "default": "doubao-pro",
            "models": {
                "doubao-pro": {
                    "driver": "ark",
                    "model": "doubao-seed-2.0-pro",
                    "api_key": "ark-test-key"
                }
            }
        }
    }

    result = check_active_llm_status(config)
    assert "用量统计暂不可用" in result
    assert "暂无调用记录" not in result
    # 坚决不输出底层 sqlite 错误栈
    assert "DatabaseError" not in result
    assert "file is not a database" not in result


def test_empty_models_dict_is_treated_as_models_schema():
    """验证空 models 字典属于新 schema，坚决不静默兜底为旧平铺配置发送请求"""
    config = {
        "llm": {
            "models": {},
            "driver": "deepseek",
            "api_key": "fake-key",
            "base_url": "https://api.deepseek.com/v1"
        }
    }
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = check_active_llm_status(config)
        mock_urlopen.assert_not_called()
        assert "配置错误" in result
        assert "已终止查询" in result


def test_session_db_path_priority_config_over_env(tmp_path, monkeypatch):
    """T2: 验证数据库解析优先级完全与 Agent 一致: 配置路径 > 环境变量 > 默认路径"""
    import sqlite3
    import time
    cfg_db = tmp_path / "cfg_instance.db"
    env_db = tmp_path / "env_instance.db"

    # 在 cfg_db 写入 5555 tokens
    c1 = sqlite3.connect(str(cfg_db))
    c1.execute("CREATE TABLE api_usage_log (model TEXT, prompt_tokens INT, completion_tokens INT, total_tokens INT, created_at REAL)")
    c1.execute("INSERT INTO api_usage_log VALUES (?, ?, ?, ?, ?)", ("m_cfg", 555, 5000, 5555, time.time()))
    c1.commit()
    c1.close()

    # 在 env_db 写入 9999 tokens
    c2 = sqlite3.connect(str(env_db))
    c2.execute("CREATE TABLE api_usage_log (model TEXT, prompt_tokens INT, completion_tokens INT, total_tokens INT, created_at REAL)")
    c2.execute("INSERT INTO api_usage_log VALUES (?, ?, ?, ?, ?)", ("m_cfg", 999, 9000, 9999, time.time()))
    c2.commit()
    c2.close()

    monkeypatch.setenv("LITE_AGENT_SESSION_DB_PATH", str(env_db))
    config = {
        "session": {"db_path": str(cfg_db)},
        "llm": {
            "default": "m",
            "models": {
                "m": {"driver": "ark", "model": "m_cfg", "api_key": "k"}
            }
        }
    }
    # 状态查询必须优先读取配置路径中的 5555，绝不能读取环境变量中的 9999
    result = check_active_llm_status(config)
    assert "5,555" in result
    assert "9,999" not in result


def test_ops_llm_skill_entry_resolves_config_db_path(tmp_path, monkeypatch):
    """T2: 验证独立用量技能入口在无显式 config 传入时，自动加载配置并使用 session.db_path"""
    import sqlite3
    import time
    from lite_agent.skills.ops_llm import llm_usage_report as skill_report

    skill_db = tmp_path / "skill_instance.db"
    conn = sqlite3.connect(str(skill_db))
    conn.execute("CREATE TABLE api_usage_log (model TEXT, prompt_tokens INT, completion_tokens INT, total_tokens INT, created_at REAL)")
    conn.execute("INSERT INTO api_usage_log VALUES (?, ?, ?, ?, ?)", ("deepseek-chat", 1000, 2000, 3000, time.time()))
    conn.commit()
    conn.close()

    fake_config = {"session": {"db_path": str(skill_db)}}
    with patch("lite_agent.core.config_loader.load_config", return_value=fake_config):
        output = skill_report(days=7)
        assert "3,000" in output
        assert "deepseek-chat" in output


def test_schema_2_endpoint_binding_in_llm_status(monkeypatch):
    """验证 Schema Version 2 端点绑定配置在状态查询中正确解析"""
    monkeypatch.setenv("TEST_DS_BIND_KEY", "sk-schema2-valid-key")
    config = {
        "llm": {
            "schema_version": 2,
            "default": "flash",
            "endpoints": {
                "ds-official": {
                    "provider": "deepseek",
                    "protocol": "openai_chat_completions",
                    "base_url": "https://api.deepseek.com/v1",
                    "credential_ref": "TEST_DS_BIND_KEY",
                    "management_ref": "deepseek-official-balance"
                }
            },
            "models": {
                "flash": {
                    "endpoint_ref": "ds-official",
                    "model": "deepseek-v4-flash"
                }
            }
        }
    }

    mock_resp_data = {
        "is_available": True,
        "balance_infos": [
            {"currency": "CNY", "total_balance": "88.88"}
        ]
    }
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(mock_resp_data).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        result = check_active_llm_status(config)
        assert "88.88" in result
        assert "CNY" in result
        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer sk-schema2-valid-key"


def test_get_today_usage_summary(tmp_path):
    """验证获取当天用量汇总能够正确统计并格式化"""
    import sqlite3
    import time
    from lite_agent.features.sys_ops.llm_ops_service import get_today_usage_summary

    db_path = str(tmp_path / "sessions.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE api_usage_log ("
        "id INTEGER PRIMARY KEY, session_key TEXT, model TEXT, "
        "prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, "
        "created_at REAL)"
    )
    now = time.time()
    yesterday = now - 86400 * 2
    # 插入今天的记录
    conn.execute(
        "INSERT INTO api_usage_log (session_key, model, prompt_tokens, completion_tokens, total_tokens, created_at) "
        "VALUES ('s1', 'deepseek-chat', 100, 50, 150, ?)", (now,)
    )
    conn.execute(
        "INSERT INTO api_usage_log (session_key, model, prompt_tokens, completion_tokens, total_tokens, created_at) "
        "VALUES ('s2', 'deepseek-chat', 200, 100, 300, ?)", (now,)
    )
    # 插入前天的记录（不计入今天）
    conn.execute(
        "INSERT INTO api_usage_log (session_key, model, prompt_tokens, completion_tokens, total_tokens, created_at) "
        "VALUES ('s3', 'deepseek-chat', 1000, 500, 1500, ?)", (yesterday,)
    )
    conn.commit()
    conn.close()

    config = {"session": {"db_path": db_path}}
    summary = get_today_usage_summary(config)
    assert "今日模型用量汇总" in summary
    assert "450" in summary  # 150 + 300
    assert "2 次调用" in summary
    assert "deepseek-chat" in summary

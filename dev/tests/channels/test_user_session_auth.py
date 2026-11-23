"""
统一用户会话鉴权、无感续期与撤销测试 (test_user_session_auth).
验证：
1. UserSessionService 签发、校验、刷新轮换（Rotation）与重用检测（Reuse Detection）；
2. 记住登录 (remember_me) 控制 Refresh Token 签发；
3. channels/api.py:_auth() 在无静态 Token 时可独立通过用户 Session 鉴权；
4. 区分未认证/已过期 (401) 与权限不足/非法凭证 (403)；
5. HTTP 端点 /api/v1/auth/refresh 与 /api/v1/auth/logout 正确处理轮换与会话族撤销。
"""

import io
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from lite_agent.channels.api import ApiHandler
from lite_agent.services.user_session import UserSessionService, DEFAULT_USER_SCOPE


def test_user_session_service_lifecycle_and_rotation():
    service = UserSessionService(db_path=":memory:")
    assert service.is_available() is True

    # 1. 签发会话 (remember_me=True)
    created = service.create_session("alice", device_id="phone_01", device_name="OnePlus 9", remember_me=True, access_ttl=3600, refresh_ttl=86400)
    access_token = created["session_token"]
    refresh_token = created["refresh_token"]
    session_id = created["session_id"]

    assert created["username"] == "alice"
    assert DEFAULT_USER_SCOPE in created["scopes"]
    assert created["expires_at"] > time.time()
    assert created["refresh_expires_at"] > created["expires_at"]

    # 2. 校验有效 Access Token
    verified, reason = service.verify_access_token(access_token)
    assert verified is not None
    assert reason is None
    assert verified["username"] == "alice"
    assert verified["session_id"] == session_id

    # 3. 校验过期 Access Token 返回 expired
    now_future = int(time.time()) + 4000
    expired_ver, expired_reason = service.verify_access_token(access_token, now=now_future)
    assert expired_ver is None
    assert expired_reason == "expired"

    # 4. 使用 Refresh Token 续期（触发轮换）
    refreshed, err = service.refresh_session(refresh_token)
    assert refreshed is not None
    assert err is None
    new_access_token = refreshed["session_token"]
    new_refresh_token = refreshed["refresh_token"]
    assert new_access_token != access_token
    assert new_refresh_token != refresh_token
    assert refreshed["session_id"] == session_id

    # 旧 access_token 应被撤销
    old_ver, old_reason = service.verify_access_token(access_token)
    assert old_ver is None
    assert old_reason == "revoked"

    # 新 access_token 应有效
    new_ver, _ = service.verify_access_token(new_access_token)
    assert new_ver is not None

    # 5. 重用检测：再次提交已被轮换的旧 refresh_token，应触发会话族吊销！
    reused, reuse_err = service.refresh_session(refresh_token)
    assert reused is None
    assert reuse_err == "reuse_detected"

    # 会话族被吊销后，刚刚轮换出的 new_access_token 与 new_refresh_token 均应失效
    rev_ver, rev_reason = service.verify_access_token(new_access_token)
    assert rev_ver is None
    assert rev_reason == "revoked"

    re_refresh, re_err = service.refresh_session(new_refresh_token)
    assert re_refresh is None


def test_user_session_remember_me_false():
    """验证 remember_me=False 时不签发长期 refresh_token。"""
    service = UserSessionService(db_path=":memory:")
    created = service.create_session("alice", remember_me=False)
    assert created["remember_me"] is False
    assert "refresh_token" not in created
    assert "refresh_expires_at" not in created
    assert created["session_token"] is not None


def test_api_handler_auth_with_session_independent_of_static_token():
    """验证 _auth() 在未配置静态 Token 时仍可独立由 UserSession 鉴权放行，且区分 401 与 403。"""
    session_service = UserSessionService(db_path=":memory:")
    created = session_service.create_session("bob", device_id="pad_01")
    valid_token = created["session_token"]

    handler = object.__new__(ApiHandler)
    handler.server = SimpleNamespace(
        api_server=SimpleNamespace(
            auth_token="",  # 未配置静态 token
            config={},
            user_session_service=session_service,
        )
    )
    handler.send_error = Mock()

    # 1. 携带合法 User Session Token -> 放行
    handler.headers = {"Authorization": f"Bearer {valid_token}"}
    assert handler._auth() is True
    assert handler.is_guest is False
    assert handler.user_session["username"] == "bob"

    # 2. 携带非法未知 Token -> 403 Forbidden
    handler.headers = {"Authorization": "Bearer invalid-token-12345678"}
    assert handler._auth() is False
    handler.send_error.assert_called_with(403, "Forbidden")

    # 3. 缺失 Authorization -> 401 Unauthorized
    handler.headers = {}
    assert handler._auth() is False
    handler.send_error.assert_called_with(401, "Unauthorized")

    # 4. 携带已撤销的 User Session Token -> 401 (引导客户端刷新)
    session_service.revoke_session(token=valid_token)
    handler.headers = {"Authorization": f"Bearer {valid_token}"}
    assert handler._auth() is False
    handler.send_error.assert_called_with(401, "Token expired or revoked")


def test_api_handler_unconfigured_auth_returns_503():
    """验证无静态 Token 且无用户会话服务时，安全降级返回 503。"""
    handler = object.__new__(ApiHandler)
    handler.server = SimpleNamespace(
        api_server=SimpleNamespace(
            auth_token="",
            config={},
            user_session_service=None,
        )
    )
    handler.send_error = Mock()
    handler.headers = {"Authorization": "Bearer test"}

    assert handler._auth() is False
    handler.send_error.assert_called_with(503, "API authentication is not configured")


def test_auth_refresh_and_logout_endpoints_with_session_family():
    """验证 HTTP 层 /api/v1/auth/refresh 与 /api/v1/auth/logout 端点支持以 access_token 撤销。"""
    from lite_agent.features.review_meeting import http_adapter

    session_service = UserSessionService(db_path=":memory:")
    created = session_service.create_session("carol", device_id="device_c", remember_me=True)
    refresh_token = created["refresh_token"]
    old_access_token = created["session_token"]

    api_server = SimpleNamespace(
        config={},
        user_session_service=session_service,
    )
    handler = SimpleNamespace(
        server=SimpleNamespace(api_server=api_server),
        headers={"Content-Length": "0"},
        rfile=io.BytesIO(),
        send_response=Mock(),
        _send_cors_headers=Mock(),
        send_header=Mock(),
        end_headers=Mock(),
        wfile=io.BytesIO(),
    )

    # 1. 调用 refresh 端点
    body = json.dumps({"refresh_token": refresh_token}).encode("utf-8")
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()

    http_adapter.handle_auth_refresh(handler)
    response_bytes = handler.wfile.getvalue()
    res = json.loads(response_bytes.decode("utf-8"))
    assert res["success"] is True
    new_access_token = res["session_token"]
    new_refresh_token = res["refresh_token"]
    assert new_access_token != old_access_token
    assert new_refresh_token != refresh_token

    # 2. 携带 access_token 调用 logout 端点
    logout_body = json.dumps({}).encode("utf-8")
    handler.headers = {
        "Content-Length": str(len(logout_body)),
        "Authorization": f"Bearer {new_access_token}"
    }
    handler.rfile = io.BytesIO(logout_body)
    handler.wfile = io.BytesIO()

    http_adapter.handle_auth_logout(handler)
    logout_res = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert logout_res["success"] is True

    # 撤销后，access_token 应已被撤销
    ver, reason = session_service.verify_access_token(new_access_token)
    assert ver is None
    assert reason == "revoked"

    # 撤销后，refresh_token 再次刷新应失败
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(json.dumps({"refresh_token": new_refresh_token}).encode("utf-8"))
    handler.wfile = io.BytesIO()

    http_adapter.handle_auth_refresh(handler)
    refreshed_after_logout = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert refreshed_after_logout["success"] is False

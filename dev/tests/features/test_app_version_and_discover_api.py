import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from lite_agent.channels.api import ApiHandler


class DummyRequest:
    def makefile(self, *args, **kwargs):
        import io
        return io.BytesIO(b"")

    def sendall(self, data):
        pass


class DummyServer:
    def __init__(self):
        self.api_server = SimpleNamespace(
            config={},
            auth_token="test-token",
            route_registry=None
        )


def create_handler(path: str, headers: dict = None):
    req = DummyRequest()
    handler = ApiHandler.__new__(ApiHandler)
    handler.connection = req
    handler.rfile = DummyRequest().makefile()
    handler.wfile = DummyRequest().makefile()
    
    import io
    handler.wfile = io.BytesIO()
    handler.server = DummyServer()
    handler.path = path
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    
    header_lines = []
    if headers:
        for k, v in headers.items():
            header_lines.append(f"{k}: {v}\r\n")
    header_str = "".join(header_lines) + "\r\n"
    from http.client import HTTPMessage
    import io
    handler.headers = HTTPMessage()
    if headers:
        for k, v in headers.items():
            handler.headers[k] = v
            
    handler._quiet = True
    return handler


def test_app_version_unauthenticated_ok():
    handler = create_handler("/agent/api/v1/app/version?currentCode=100001&channel=debug")
    handler.do_GET()
    
    output = handler.wfile.getvalue().decode('utf-8', errors='ignore')
    assert "200" in output or "hasUpdate" in output
    idx = output.find('{')
    assert idx != -1
    data = json.loads(output[idx:])
    assert "hasUpdate" in data
    assert data["channel"] == "debug"
    assert data["versionCode"] >= 100001


def test_billing_summary_authenticated_ok():
    handler = create_handler("/agent/api/v1/billing/summary", headers={"Authorization": "Bearer test-token"})
    handler.do_GET()
    
    output = handler.wfile.getvalue().decode('utf-8', errors='ignore')
    idx = output.find('{')
    assert idx != -1
    data = json.loads(output[idx:])
    assert data["success"] is True
    assert "total_statements" in data
    assert "unpaid_count" in data


def test_mail_recent_authenticated_ok():
    handler = create_handler("/agent/api/v1/mail/recent?limit=10", headers={"Authorization": "Bearer test-token"})
    handler.do_GET()
    
    output = handler.wfile.getvalue().decode('utf-8', errors='ignore')
    idx = output.find('{')
    assert idx != -1
    data = json.loads(output[idx:])
    assert data["success"] is True
    assert "emails" in data

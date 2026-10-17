"""Functional regressions for defects recorded after stage-three risk acceptance."""

from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from app.bootstrap import create_app
from channels.api import ApiHandler


@contextmanager
def dashboard_endpoint(app, agent=None):
    class QuietHandler(ApiHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    server.api_server = SimpleNamespace(
        agent=agent if agent is not None else SimpleNamespace(command_registry=app.command_registry),
        route_registry=app.route_registry,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/v1/dashboard"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def fetch_dashboard(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.status == 200
        return json.load(response)


def test_core_dashboard_uses_its_own_registry_not_legacy_global():
    from core.command_registry import _registry

    app = create_app(mode="core")
    _registry.register("/foreign_dashboard_probe", lambda *_: None, show_in_dashboard=True)
    try:
        with dashboard_endpoint(app) as url:
            assert fetch_dashboard(url) == app.command_registry.items_for_dashboard()
            assert {item["name"] for item in fetch_dashboard(url)} == {"/new"}
    finally:
        _registry.unregister("/foreign_dashboard_probe")
        app.shutdown()


def test_full_plugin_dashboard_and_parallel_core_are_isolated():
    core = create_app(mode="core")
    full = create_app(mode="full")
    try:
        full.plugin_loader.load_plugin_by_entry("features.content_ops", "content_ops")
        with dashboard_endpoint(core) as core_url, dashboard_endpoint(full) as full_url:
            core_items = fetch_dashboard(core_url)
            full_items = fetch_dashboard(full_url)
            assert {item["name"] for item in core_items} == {"/new"}
            assert full_items == full.command_registry.items_for_dashboard()
            assert {"/new", "/rss_list", "/rss_topic"} <= {item["name"] for item in full_items}
            assert "/rss_fetch" not in {item["name"] for item in full_items}
            assert all(set(item) == {"name", "description"} for item in full_items)
    finally:
        full.shutdown()
        core.shutdown()


def test_dashboard_missing_host_registry_is_explicitly_unavailable():
    app = create_app(mode="core")
    try:
        with dashboard_endpoint(app, agent=SimpleNamespace()) as url:
            with pytest.raises(urllib.error.HTTPError) as exc:
                fetch_dashboard(url)
            assert exc.value.code == 503
    finally:
        app.shutdown()


def test_backup_star_import_resolves_existing_exports_without_running_backup():
    from features.sys_ops import backup_service

    exported = {}
    exec("from features.sys_ops.backup_service import *", exported)
    assert set(exported) - {"__builtins__"} == set(backup_service.__all__)
    for name in backup_service.__all__:
        assert exported[name] is getattr(backup_service, name)


@pytest.mark.parametrize("prefix", ["/", "::"])
def test_legacy_host_still_dispatches_global_compatibility_commands(prefix):
    from agent import Agent
    from core.command_registry import _registry

    calls = []

    def handler(agent, msg, args):
        calls.append((msg.text, args))
        return "legacy response"

    _registry.register("/legacy_dispatch_probe", handler, guest_ok=False)
    try:
        host = Agent.__new__(Agent)  # Older callers may not inject an instance registry.
        msg = SimpleNamespace(text=prefix + "legacy_dispatch_probe value", is_guest=False)
        method = host._handle_builtin if prefix == "/" else host._handle_double_colon
        assert method(msg).text == "legacy response"
        assert calls == [(msg.text, ["value"])]
    finally:
        _registry.unregister("/legacy_dispatch_probe")

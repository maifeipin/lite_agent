import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from channels.api import ApiHandler
from core.task_spec_service import TaskSpecRevisionConflict


def test_send_json_supports_non_200_status_and_content_length():
    handler = ApiHandler.__new__(ApiHandler)
    handler.wfile = io.BytesIO()
    statuses = []
    headers = {}
    handler.send_response = statuses.append
    handler._send_cors_headers = lambda: None
    handler.send_header = lambda name, value: headers.__setitem__(name, value)
    handler.end_headers = lambda: None

    handler._send_json({"error": "timeout"}, 504)

    body = handler.wfile.getvalue()
    assert statuses == [504]
    assert json.loads(body) == {"error": "timeout"}
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Content-Length"] == str(len(body))


def test_send_json_ignores_client_disconnect_after_work_completed():
    class DisconnectedWriter:
        def write(self, body):
            raise BrokenPipeError("browser refreshed")

    handler = ApiHandler.__new__(ApiHandler)
    handler.wfile = DisconnectedWriter()
    handler.send_response = lambda status: None
    handler._send_cors_headers = lambda: None
    handler.send_header = lambda name, value: None
    handler.end_headers = lambda: None

    assert handler._send_json({"ok": True}, 201) is False
    assert handler._quiet is True


def test_generate_compatibility_endpoint_persists_without_calling_llm():
    task_specs = MagicMock()
    task_specs.create_manual.return_value = {
        "id": "task-1", "status": "review_required", "spec": {},
    }
    handler = ApiHandler.__new__(ApiHandler)
    handler.server = SimpleNamespace(
        api_server=SimpleNamespace(task_specs=task_specs)
    )
    handler._read_json_body = lambda: {
        "goal": "查看一下最近的外币账单", "name": "外币账单",
    }
    responses = []
    handler._send_json = lambda value, status=200: responses.append((value, status))

    handler._handle_task_spec_generate()

    task_specs.create_manual.assert_called_once_with(
        "查看一下最近的外币账单", "外币账单"
    )
    task_specs.generate.assert_not_called()
    assert responses[0][1] == 201
    assert responses[0][0]["generation"]["status"] == "not_started"


def test_enrich_revision_conflict_is_returned_as_http_409():
    task_specs = MagicMock()
    task_specs.enrich.side_effect = TaskSpecRevisionConflict("用户版本已变化")
    handler = ApiHandler.__new__(ApiHandler)
    handler.server = SimpleNamespace(
        api_server=SimpleNamespace(task_specs=task_specs)
    )
    handler.headers = {"Content-Length": "0"}
    responses = []
    handler._send_json = lambda value, status=200: responses.append((value, status))

    handler._handle_task_spec_action("/api/v1/task-specs/task-1/enrich")

    assert responses == [({
        "error": "用户版本已变化", "code": "REVISION_CONFLICT",
    }, 409)]


def test_enrich_action_forwards_one_time_model():
    task_specs = MagicMock()
    task_specs.enrich.return_value = {"id": "task-1"}
    handler = ApiHandler.__new__(ApiHandler)
    handler.server = SimpleNamespace(
        api_server=SimpleNamespace(task_specs=task_specs)
    )
    handler.headers = {"Content-Length": "24"}
    handler._read_json_body = lambda: {"model": "gemini-pro"}
    responses = []
    handler._send_json = lambda value, status=200: responses.append((value, status))

    handler._handle_task_spec_action("/api/v1/task-specs/task-1/enrich")

    task_specs.enrich.assert_called_once_with("task-1", "gemini-pro")
    assert responses == [({"id": "task-1"}, 200)]


def test_manual_task_cannot_enable_schedule():
    task_specs = MagicMock()
    task_specs.store.get.return_value = {
        "id": "task-1", "status": "approved",
        "spec": {"execution": {"schedule": {"mode": "manual"}}},
    }
    handler = ApiHandler.__new__(ApiHandler)
    handler.server = SimpleNamespace(
        api_server=SimpleNamespace(task_specs=task_specs)
    )
    handler.headers = {"Content-Length": "17"}
    handler._read_json_body = lambda: {"enabled": True}
    responses = []
    handler._send_json = lambda value, status=200: responses.append((value, status))

    handler._handle_task_spec_action("/api/v1/task-specs/task-1/schedule")

    assert responses == [({
        "error": "手动任务没有调度时间，请先设置一次或重复计划",
    }, 400)]
    task_specs.store.save.assert_not_called()

import io
import json

from channels.api import ApiHandler


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

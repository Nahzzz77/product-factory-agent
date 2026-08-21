from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from product_factory.errors import FactoryError
from product_factory.web.app import create_server, run_console
from product_factory.web.service import ConsoleService


FACTORY_ROOT = Path(__file__).resolve().parents[2]


def _request(server, method: str, path: str, *, token: str | None = None, body: dict | None = None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Product-Factory-Token"] = token
    connection.request(method, path, body=json.dumps(body).encode() if body else None, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, response.getheader("Content-Type"), payload


def test_http_api_requires_token_and_serves_dashboard(tmp_path: Path) -> None:
    service = ConsoleService(tmp_path, factory_root=FACTORY_ROOT)
    server = create_server("127.0.0.1", 0, service=service, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, content_type, body = _request(server, "GET", "/")
        assert status == 200
        assert content_type.startswith("text/html")
        assert "产品工厂" in body.decode("utf-8")
        assert "test-token" not in body.decode("utf-8")

        status, _, body = _request(server, "GET", "/api/config")
        assert status == 403
        assert json.loads(body)["error"]["code"] == "web_token_required"

        status, _, body = _request(server, "GET", "/api/config", token="test-token")
        assert status == 200
        assert json.loads(body)["workspace"] == str(tmp_path.resolve())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_http_rejects_oversized_json(tmp_path: Path) -> None:
    service = ConsoleService(tmp_path, factory_root=FACTORY_ROOT)
    server = create_server("127.0.0.1", 0, service=service, token="test-token", max_body_bytes=16)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _request(
            server, "POST", "/api/projects", token="test-token", body={"large": "x" * 100}
        )
        assert status == 413
        assert json.loads(body)["error"]["code"] == "request_too_large"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_console_rejects_network_listener_without_explicit_authorization(tmp_path: Path) -> None:
    with pytest.raises(FactoryError, match="web_network_not_allowed"):
        run_console(tmp_path, host="0.0.0.0", open_browser=False)

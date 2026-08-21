"""Dependency-free, loopback-only HTTP server for the local console."""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from product_factory.cli.output import _normalise
from product_factory.errors import ErrorCategory, FactoryError
from product_factory.web.service import ConsoleService


def main() -> int:
    parser = argparse.ArgumentParser(prog="product-factory-web", description="产品工厂本地工作台")
    parser.add_argument("--workspace", default=str(Path.home() / "ProductFactoryProjects"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    arguments = parser.parse_args()
    try:
        run_console(
            Path(arguments.workspace), host=arguments.host, port=arguments.port,
            open_browser=not arguments.no_open, allow_network=arguments.allow_network,
        )
    except KeyboardInterrupt:
        return 0
    return 0


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler,
        *,
        service: ConsoleService,
        token: str,
        max_body_bytes: int,
    ) -> None:
        super().__init__(address, handler)
        self.service = service
        self.token = token
        self.max_body_bytes = max_body_bytes


def create_server(
    host: str,
    port: int,
    *,
    service: ConsoleService,
    token: str | None = None,
    max_body_bytes: int = 4_500_000,
) -> ConsoleHTTPServer:
    return ConsoleHTTPServer(
        (host, port), ConsoleRequestHandler, service=service,
        token=token or secrets.token_urlsafe(32), max_body_bytes=max_body_bytes,
    )


def run_console(
    workspace: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    allow_network: bool = False,
) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"} and not allow_network:
        raise FactoryError(
            "web_network_not_allowed", ErrorCategory.POLICY_BLOCKED,
            "默认只能在本机访问 Web 工作台", "web", False,
            "使用 127.0.0.1，或明确添加 --allow-network",
        )
    if not 0 <= port <= 65535:
        raise FactoryError(
            "web_port_invalid", ErrorCategory.INPUT_REQUIRED,
            "Web 工作台端口无效", "web", False, "使用 0 到 65535 之间的端口",
        )
    service = ConsoleService(workspace)
    server = create_server(host, port, service=service)
    actual_host, actual_port = server.server_address[:2]
    display_host = "127.0.0.1" if actual_host in {"0.0.0.0", "::"} else actual_host
    url = f"http://{display_host}:{actual_port}/"
    print(f"产品工厂本地工作台：{url}")
    print(f"项目工作区：{service.workspace}")
    print("按 Ctrl+C 停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        server.server_close()


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    server: ConsoleHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._static("index.html", "text/html; charset=utf-8", set_cookie=True)
            return
        if parsed.path == "/app.css":
            self._static("app.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/app.js":
            self._static("app.js", "text/javascript; charset=utf-8")
            return
        if parsed.path.startswith("/api/") and not self._authorized():
            self._error(HTTPStatus.FORBIDDEN, "web_token_required", "本地会话令牌缺失")
            return
        if parsed.path == "/api/config":
            self._json(HTTPStatus.OK, self.server.service.config())
            return
        if parsed.path == "/api/projects":
            self._json(HTTPStatus.OK, {"projects": self.server.service.list_projects()})
            return
        if parsed.path == "/api/project":
            query = parse_qs(parsed.query)
            self._call(lambda: self.server.service.snapshot(query.get("path", [""])[0]))
            return
        if parsed.path == "/api/agent-runs":
            query = parse_qs(parsed.query)
            self._call(
                lambda: {
                    "runs": self.server.service.agent_history(
                        query.get("project_path", [""])[0]
                    )
                }
            )
            return
        if parsed.path.startswith("/api/agent-runs/"):
            run_id = parsed.path.rsplit("/", 1)[-1]
            query = parse_qs(parsed.query)
            self._call(
                lambda: self.server.service.get_agent_run(
                    query.get("project_path", [""])[0], run_id
                )
            )
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "页面不存在")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "not_found", "页面不存在")
            return
        if not self._authorized():
            self._error(HTTPStatus.FORBIDDEN, "web_token_required", "本地会话令牌缺失")
            return
        payload = self._read_json()
        if payload is None:
            return
        if parsed.path == "/api/projects":
            self._call(lambda: self.server.service.create_project(payload), status=HTTPStatus.CREATED)
            return
        if parsed.path == "/api/action":
            self._call(
                lambda: self.server.service.perform_action(
                    str(payload.get("project_path", "")), str(payload.get("action", "")), payload
                )
            )
            return
        if parsed.path == "/api/agent-runs":
            self._call(
                lambda: self.server.service.start_agent(
                    str(payload.get("project_path", "")), str(payload.get("objective", ""))
                ),
                status=HTTPStatus.ACCEPTED,
            )
            return
        if parsed.path.startswith("/api/agent-runs/") and parsed.path.endswith("/cancel"):
            run_id = parsed.path.split("/")[-2]
            self._call(
                lambda: self.server.service.cancel_agent(
                    str(payload.get("project_path", "")), run_id
                )
            )
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _call(self, function, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        try:
            self._json(status, {"ok": True, "data": function()})
        except FactoryError as error:
            self._json(
                HTTPStatus.CONFLICT if error.category.value != "input_required" else HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": _normalise(error)},
            )
        except (OSError, ValueError):
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "操作未能安全完成")

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "request_invalid", "请求长度无效")
            return None
        if length > self.server.max_body_bytes:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "请求内容过大")
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "request_invalid", "请求不是有效 JSON")
            return None
        if not isinstance(payload, dict):
            self._error(HTTPStatus.BAD_REQUEST, "request_invalid", "请求必须是 JSON 对象")
            return None
        return payload

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Product-Factory-Token", "")
        if not supplied:
            cookies = self.headers.get("Cookie", "")
            for item in cookies.split(";"):
                key, separator, value = item.strip().partition("=")
                if separator and key == "pf_console_token":
                    supplied = value
                    break
        return hmac.compare_digest(supplied, self.server.token)

    def _static(self, name: str, content_type: str, *, set_cookie: bool = False) -> None:
        content = files("product_factory.web.static").joinpath(name).read_bytes()
        headers = {
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        if set_cookie:
            headers["Set-Cookie"] = (
                f"pf_console_token={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
            )
        self._send(HTTPStatus.OK, content, headers)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        content = json.dumps(_normalise(payload), ensure_ascii=False).encode("utf-8")
        self._send(
            status,
            content,
            {
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"ok": False, "error": {"code": code, "message": message}})

    def _send(self, status: HTTPStatus, content: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

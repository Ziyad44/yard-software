"""Local dashboard web server for the smart yard backend."""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .dashboard_runtime import DashboardRuntime


MODULE_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = MODULE_ROOT / "static"
DASHBOARD_HTML_PATH = STATIC_ROOT / "dashboard" / "index.html"


def _load_dashboard_html() -> str:
    if DASHBOARD_HTML_PATH.is_file():
        return DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    return """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><title>Dashboard Missing</title></head>
<body>Missing dashboard static file: yard/static/dashboard/index.html</body>
</html>"""


HTML_PAGE = _load_dashboard_html()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler exposing dashboard page, static assets, and JSON endpoints."""

    runtime: DashboardRuntime
    lock: threading.Lock

    def _write_bytes(
        self,
        body: bytes,
        *,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._write_bytes(
            body,
            content_type="application/json; charset=utf-8",
            status=status,
        )

    def _write_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._write_bytes(
            body.encode("utf-8"),
            content_type="text/html; charset=utf-8",
            status=status,
        )

    def _parse_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    @staticmethod
    def _resolve_static_file(path: str) -> Path | None:
        if not path.startswith("/static/"):
            return None
        relative_path = path.removeprefix("/static/").lstrip("/")
        if not relative_path:
            return None

        candidate = (STATIC_ROOT / relative_path).resolve()
        static_root = STATIC_ROOT.resolve()
        try:
            candidate.relative_to(static_root)
        except ValueError:
            return None
        return candidate

    def _serve_static(self, path: str) -> bool:
        candidate = self._resolve_static_file(path)
        if candidate is None:
            return False
        if not candidate.is_file():
            self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return True

        mime_type, _ = mimetypes.guess_type(str(candidate))
        if mime_type is None:
            mime_type = "application/octet-stream"
        if mime_type.startswith("text/") or mime_type in {"application/javascript", "application/json"}:
            mime_type = f"{mime_type}; charset=utf-8"

        self._write_bytes(candidate.read_bytes(), content_type=mime_type, status=HTTPStatus.OK)
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._write_html(HTML_PAGE)
            return
        if self._serve_static(path):
            return
        if path == "/api/state":
            with self.lock:
                payload = self.runtime.get_dashboard_payload()
            self._write_json(payload)
            return
        self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._parse_json_body()
            with self.lock:
                if path == "/api/step":
                    minutes = int(payload.get("minutes", 1))
                    result = self.runtime.step(minutes=minutes)
                elif path == "/api/supervisor":
                    result = self.runtime.update_supervisor(payload)
                elif path == "/api/recommendation/apply":
                    result = self.runtime.apply_recommendation()
                elif path == "/api/recommendation/keep":
                    result = self.runtime.keep_current_plan()
                else:
                    self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                    return
            self._write_json(result)
        except ValueError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self._write_json({"error": "invalid json payload"}, status=HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def _build_handler(runtime: DashboardRuntime, lock: threading.Lock) -> type[DashboardRequestHandler]:
    class Handler(DashboardRequestHandler):
        pass

    Handler.runtime = runtime
    Handler.lock = lock
    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local yard dashboard server.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8787, help="Port to bind (default: 8787).")
    args = parser.parse_args()

    runtime = DashboardRuntime.create_default()
    lock = threading.Lock()
    handler = _build_handler(runtime, lock)
    server = ThreadingHTTPServer((args.host, args.port), handler)

    print(f"Dashboard running on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

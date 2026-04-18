import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from yard.dashboard_runtime import DashboardRuntime
from yard.dashboard_server import HTML_PAGE, _build_handler


def _get_json(base_url: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _post_json(base_url: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def test_dashboard_server_api_flow() -> None:
    runtime = DashboardRuntime.create_default()
    handler = _build_handler(runtime, threading.Lock())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        base_url = f"http://{host}:{port}"

        state_payload = _get_json(base_url, "/api/state")
        assert "kpis" in state_payload

        stepped = _post_json(base_url, "/api/step", {"minutes": 1})
        assert stepped["minute"] == state_payload["minute"] + 1

        updated = _post_json(
            base_url,
            "/api/supervisor",
            {
                "available_workers": 5,
                "available_forklifts": 2,
                "active_docks": 3,
                "max_unloaders_per_dock": 3,
            },
        )
        assert updated["supervisor_inputs"]["available_workers"] == 5
        assert updated["supervisor_inputs"]["active_docks"] == 3
        assert updated["supervisor_inputs"]["max_unloaders_per_dock"] == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_html_removes_manual_override_copy() -> None:
    assert "Optional manual dock overrides are deferred in v1 to keep controls simple." not in HTML_PAGE


def test_dashboard_html_includes_readable_chart_axes_and_labels() -> None:
    assert "function renderLineChart" in HTML_PAGE
    assert "yTicks" in HTML_PAGE
    assert "t=${xStart}" in HTML_PAGE
    assert "t=${xEnd}" in HTML_PAGE
    assert "text-anchor=\"end\"" in HTML_PAGE
    assert "yLabel" in HTML_PAGE

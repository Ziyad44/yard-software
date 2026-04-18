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


def test_dashboard_html_has_five_required_operation_sections() -> None:
    assert "Live Operations" in HTML_PAGE
    assert "Dock &amp; Staging" in HTML_PAGE
    assert "Resource Panel" in HTML_PAGE
    assert "Recommendation Panel" in HTML_PAGE
    assert "Analytics" in HTML_PAGE
    assert "id=\"liveOperationsSection\"" in HTML_PAGE
    assert "id=\"dockStagingSection\"" in HTML_PAGE
    assert "id=\"resourcePanelSection\"" in HTML_PAGE
    assert "id=\"recommendationPanelSection\"" in HTML_PAGE
    assert "id=\"analyticsSection\"" in HTML_PAGE


def test_dashboard_html_includes_queue_and_gate_history_page() -> None:
    assert "Queue &amp; Gate History" in HTML_PAGE
    assert "id=\"navQueueHistory\"" in HTML_PAGE
    assert "id=\"queueHistoryPage\"" in HTML_PAGE
    assert "id=\"queueTableBody\"" in HTML_PAGE
    assert "id=\"gateHistoryTableBody\"" in HTML_PAGE


def test_dashboard_html_includes_theme_toggle_and_static_module_wiring() -> None:
    assert "id=\"themeToggle\"" in HTML_PAGE
    assert "/static/dashboard/app.css" in HTML_PAGE
    assert "/static/dashboard/main.js" in HTML_PAGE


def test_dashboard_server_serves_static_assets() -> None:
    runtime = DashboardRuntime.create_default()
    handler = _build_handler(runtime, threading.Lock())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        with urllib.request.urlopen(f"{base_url}/static/dashboard/main.js", timeout=5) as response:  # noqa: S310
            body = response.read().decode("utf-8")
            content_type = response.headers.get("Content-Type", "")
            assert "import { apiGet, apiPost }" in body
            assert "javascript" in content_type

        with urllib.request.urlopen(f"{base_url}/static/dashboard/app.css", timeout=5) as response:  # noqa: S310
            body = response.read().decode("utf-8")
            content_type = response.headers.get("Content-Type", "")
            assert ":root" in body
            assert "text/css" in content_type
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from yard.dashboard_runtime import DashboardRuntime
from yard.dashboard_server import _build_handler


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


def test_dashboard_api_contract_state_and_lifecycle() -> None:
    runtime = DashboardRuntime.create_default()
    handler = _build_handler(runtime, threading.Lock())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        base_url = f"http://{host}:{port}"

        initial = _get_json(base_url, "/api/state")
        assert "kpis" in initial
        assert "live_operations" in initial
        assert "dock_status" in initial
        assert "trends" in initial
        assert "queue_table" in initial
        assert "gate_history" in initial
        assert "spec_3" in initial["verification"]
        assert "spec_4" in initial["verification"]
        assert "throughput_trucks_per_hour" in initial["kpis"]
        assert "dock_utilization_pct" in initial["trends"]
        if initial["recommendation"]["minute_generated"] is None:
            assert initial["recommendation"]["text"] == "No active recommendation."
            assert initial["recommendation"]["rationale"] == "Waiting for next trigger."
            assert initial["recommendation"]["decision_status"] == "none"
            assert initial["recommendation"]["is_applied"] is False
        initial_trend_len = len(initial["trends"]["minutes"])

        stepped = _post_json(base_url, "/api/step", {"minutes": 1})
        assert stepped["minute"] == initial["minute"] + 1
        assert len(stepped["trends"]["minutes"]) == initial_trend_len + 1

        run15 = _post_json(base_url, "/api/step", {"minutes": 15})
        assert run15["minute"] == stepped["minute"] + 15
        assert len(run15["trends"]["minutes"]) == len(stepped["trends"]["minutes"]) + 15

        # Review timer must have produced at least one recommendation by now.
        assert run15["recommendation"]["minute_generated"] is not None

        kept = _post_json(base_url, "/api/recommendation/keep", {})
        assert kept["recommendation"]["is_applied"] is False
        assert kept["recommendation"]["decision_status"] == "kept_current_plan"

        applied = _post_json(base_url, "/api/recommendation/apply", {})
        assert applied["recommendation"]["is_applied"] is True
        assert applied["recommendation"]["decision_status"] == "applied"

        updated = _post_json(
            base_url,
            "/api/supervisor",
            {
                "available_workers": 4,
                "available_forklifts": 2,
                "active_docks": 2,
                "max_unloaders_per_dock": 2,
            },
        )
        assert updated["supervisor_inputs"]["available_workers"] == 4
        assert updated["supervisor_inputs"]["available_forklifts"] == 2
        assert updated["supervisor_inputs"]["max_unloaders_per_dock"] == 2
        assert all(row["assigned_workers"] <= 2 for row in updated["dock_status"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_keep_endpoint_without_recommendation_returns_400() -> None:
    runtime = DashboardRuntime.create_default()
    handler = _build_handler(runtime, threading.Lock())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        base_url = f"http://{host}:{port}"

        body = json.dumps({}).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/api/recommendation/keep",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)  # noqa: S310
            raise AssertionError("Expected HTTP 400 when no recommendation exists")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

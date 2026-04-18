from __future__ import annotations

import contextlib
import dataclasses
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from yard.config import YardConfig
from yard.dashboard_runtime import DashboardRuntime, TrendPoint
from yard.dashboard_server import _build_handler
from yard.engine import initialize_state, refresh_kpi_cache
from yard.models import Action, Recommendation, TriggerType, Truck
from yard.simulation import sanitize_assignment_for_dock


DEFAULT_SUPERVISOR: dict[str, int] = {
    "active_docks": 4,
    "available_workers": 5,
    "available_forklifts": 3,
    "max_unloaders_per_dock": 4,
}


VALID_TRIGGER_TYPES: set[TriggerType] = {"dock_freed", "staging_threshold", "review_timer"}


def make_seeded_runtime(
    *,
    seed: int = 23,
    config_overrides: dict[str, Any] | None = None,
    supervisor_overrides: dict[str, int] | None = None,
) -> DashboardRuntime:
    supervisor = dict(DEFAULT_SUPERVISOR)
    if supervisor_overrides:
        supervisor.update(supervisor_overrides)

    config_kwargs: dict[str, Any] = {
        "arrival_rate_per_hour": 0.0,
        "review_interval_minutes": 6,
        "lookahead_horizon_minutes": 20,
        "max_unloaders_per_dock": supervisor["max_unloaders_per_dock"],
    }
    if config_overrides:
        config_kwargs.update(config_overrides)
    config = YardConfig(**config_kwargs)

    state = initialize_state(
        available_workers=supervisor["available_workers"],
        available_forklifts=supervisor["available_forklifts"],
        active_docks=supervisor["active_docks"],
        max_unloaders_per_dock=supervisor["max_unloaders_per_dock"],
        config=config,
    )
    return DashboardRuntime(config=config, state=state, rng_seed=seed)


def seed_yard_state(
    runtime: DashboardRuntime,
    *,
    dock_rows: list[dict[str, Any]],
    queue_rows: list[dict[str, Any]],
) -> None:
    state = runtime.state
    state.waiting_queue.clear()
    state.last_recommendation = None
    state.hold_gate_release = False
    runtime.last_recommendation_minute = None
    runtime.recommendation_trigger_batch = []
    runtime.last_trigger_batch = []
    runtime.recommendation_applied = False
    runtime.recommendation_decision = "none"

    row_by_dock = {int(row["dock_id"]): row for row in dock_rows}
    for dock_id, dock in state.docks.items():
        row = row_by_dock.get(dock_id, {})
        dock.active = bool(row.get("active", True))
        dock.assigned_workers = int(row.get("workers", dock.assigned_workers))
        dock.assigned_forklifts = int(row.get("forklifts", dock.assigned_forklifts))
        dock.staging.occupancy_units = float(row.get("staging", 0.0))
        dock.staging.capacity_units = 100.0
        dock.staging.threshold_alert_active = bool(row.get("threshold_alert_active", False))
        truck_type = row.get("truck_type")
        if truck_type is None:
            dock.current_truck = None
            if dock.staging.occupancy_units <= 0.0:
                dock.staging.load_family = None
            elif "staging_load_family" in row:
                dock.staging.load_family = row["staging_load_family"]
            elif dock.assigned_workers > 0 and dock.assigned_forklifts <= 0:
                dock.staging.load_family = "floor"
            elif dock.assigned_forklifts > 0 and dock.assigned_workers <= 0:
                dock.staging.load_family = "palletized"
            else:
                dock.staging.load_family = "floor"
        else:
            remaining = float(row["truck_remaining"])
            initial = float(row.get("truck_initial", max(remaining, 1.0)))
            truck_id = str(row.get("truck_id", f"D{dock_id}-TRUCK"))
            dock.current_truck = Truck(
                truck_id=truck_id,
                truck_type=truck_type,
                initial_load_units=initial,
                remaining_load_units=remaining,
                gate_arrival_minute=state.now_minute,
                assigned_dock_id=dock_id,
                unload_start_minute=state.now_minute,
            )
            dock.staging.load_family = "floor" if truck_type.endswith("_floor") else "palletized"

        normalized_workers, normalized_forklifts = sanitize_assignment_for_dock(
            dock=dock,
            workers=dock.assigned_workers,
            forklifts=dock.assigned_forklifts,
            max_unloaders_per_dock=runtime.config.max_unloaders_per_dock,
        )
        dock.assigned_workers = normalized_workers
        dock.assigned_forklifts = normalized_forklifts

    for idx, row in enumerate(queue_rows, start=1):
        truck_type = row["truck_type"]
        remaining = float(row.get("remaining", row.get("initial", runtime.config.truck_load_units[truck_type])))
        initial = float(row.get("initial", max(remaining, 1.0)))
        truck_id = str(row.get("truck_id", f"Q{idx:03d}"))
        state.waiting_queue.append(
            Truck(
                truck_id=truck_id,
                truck_type=truck_type,
                initial_load_units=initial,
                remaining_load_units=remaining,
                gate_arrival_minute=state.now_minute,
            )
        )

    state.update_resource_assignment_counters()
    runtime._enforce_idle_dock_zero_assignments()
    state.active_action = Action(
        action_name="seeded_plan",
        workers_by_dock={dock_id: max(dock.assigned_workers, 0) for dock_id, dock in state.docks.items() if dock.active},
        forklifts_by_dock={
            dock_id: max(dock.assigned_forklifts, 0) for dock_id, dock in state.docks.items() if dock.active
        },
        hold_gate_release=False,
        notes="Seeded runtime plan for deterministic tests.",
    )
    refresh_kpi_cache(state, runtime.config)
    runtime.trend_history = [
        TrendPoint(
            minute=state.now_minute,
            queue_length=state.queue_length,
            arrivals=0,
            max_staging_occupancy_pct=round(
                max((100.0 * dock.staging.occupancy_ratio for dock in state.docks.values() if dock.active), default=0.0),
                1,
            ),
        )
    ]


def step_minutes_via_runtime(runtime: DashboardRuntime, minutes: int) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for _ in range(minutes):
        payload = runtime.step(1)
        assert_dashboard_matches_backend(runtime, payload)
        payloads.append(payload)
    return payloads


def run_minutes_via_runtime(runtime: DashboardRuntime, minutes: int) -> dict[str, Any]:
    payload = runtime.step(minutes)
    assert_dashboard_matches_backend(runtime, payload)
    return payload


def assert_dashboard_matches_backend(runtime: DashboardRuntime, payload: dict[str, Any]) -> None:
    state = runtime.state

    for required in (
        "minute",
        "supervisor_inputs",
        "kpis",
        "recommendation",
        "staging_status",
        "dock_status",
        "resource_summary",
        "verification",
        "trends",
    ):
        assert required in payload

    assert payload["minute"] == state.now_minute
    supervisor = payload["supervisor_inputs"]
    assert supervisor["active_docks"] == sum(1 for dock in state.docks.values() if dock.active)
    assert supervisor["available_workers"] == state.resources.total_workers
    assert supervisor["available_forklifts"] == state.resources.total_forklifts
    assert supervisor["max_unloaders_per_dock"] == runtime.config.max_unloaders_per_dock

    staging_by_dock = {int(card["dock_id"]): card for card in payload["staging_status"]}
    dock_rows = {int(row["dock_id"]): row for row in payload["dock_status"]}
    for dock_id, dock in state.docks.items():
        if not dock.active:
            continue
        assert dock_id in staging_by_dock
        assert dock_id in dock_rows
        card = staging_by_dock[dock_id]
        row = dock_rows[dock_id]

        occupancy_pct = 0.0 if dock.staging.capacity_units <= 0.0 else 100.0 * dock.staging.occupancy_units / dock.staging.capacity_units
        assert card["occupancy_units"] == pytest.approx(round(dock.staging.occupancy_units, 1), abs=0.11)
        assert card["occupancy_pct"] == pytest.approx(round(occupancy_pct, 1), abs=0.11)
        expected_light = "red" if occupancy_pct >= 85.0 else "yellow" if occupancy_pct >= 70.0 else "green"
        assert card["traffic_light"] == expected_light

        assert row["assigned_workers"] == dock.assigned_workers
        assert row["assigned_forklifts"] == dock.assigned_forklifts
        assert row["staging_occupancy_units"] == pytest.approx(round(dock.staging.occupancy_units, 1), abs=0.11)
        assert row["staging_occupancy_pct"] == pytest.approx(round(occupancy_pct, 1), abs=0.11)
        expected_status = "busy" if dock.phase != "idle" else "idle"
        assert row["status"] == expected_status
        if dock.current_truck is None:
            assert row["truck_id"] is None
            assert row["truck_type"] is None
        else:
            assert row["truck_id"] == dock.current_truck.truck_id
            assert row["truck_type"] == dock.current_truck.truck_type

    resources = payload["resource_summary"]
    assert resources["workers_total"] == state.resources.total_workers
    assert resources["workers_assigned"] == state.resources.assigned_workers
    assert resources["workers_idle"] == state.resources.idle_workers
    assert resources["forklifts_total"] == state.resources.total_forklifts
    assert resources["forklifts_assigned"] == state.resources.assigned_forklifts
    assert resources["forklifts_idle"] == state.resources.idle_forklifts

    rec_payload = payload["recommendation"]
    rec_state: Recommendation | None = state.last_recommendation
    if rec_state is None:
        assert rec_payload["text"] == "No active recommendation."
        assert rec_payload["rationale"] == "Waiting for next trigger."
        assert rec_payload["decision_status"] == "none"
        assert rec_payload["is_applied"] is False
        assert rec_payload["trigger_source"] == []
    else:
        assert rec_payload["text"] == rec_state.rationale
        assert rec_payload["rationale"] == rec_state.rationale
        assert rec_payload["is_applied"] is runtime.recommendation_applied
        assert rec_payload["decision_status"] == runtime.recommendation_decision
    assert rec_payload["minute_generated"] == runtime.last_recommendation_minute

    trigger_source_events = runtime.recommendation_trigger_batch if rec_state is not None else []
    expected_sources = [
        f"{event.trigger_type} (dock {event.dock_id})" if event.dock_id else event.trigger_type
        for event in trigger_source_events
    ]
    assert rec_payload["trigger_source"] == expected_sources

    verification = payload["verification"]
    assert "spec_3" in verification
    assert "spec_4" in verification
    for key in ("spec_3", "spec_4"):
        card = verification[key]
        assert "title" in card
        assert "status" in card
        assert "current_value" in card
        assert "target" in card

    trends = payload["trends"]
    length = len(trends["minutes"])
    assert length >= 1
    assert length == len(trends["queue_length"]) == len(trends["arrivals"]) == len(trends["max_staging_occupancy_pct"])
    assert trends["minutes"][-1] == state.now_minute

    # Global invariants
    assert all(0.0 <= dock.staging.occupancy_units <= 100.0 for dock in state.docks.values())
    for dock in state.docks.values():
        if dock.current_truck is not None:
            assert dock.current_truck.remaining_load_units >= 0.0
        if dock.staging.occupancy_units > 0.0:
            assert not dock.can_accept_next_truck()
        if (not dock.active) or dock.phase == "idle":
            assert dock.assigned_workers == 0
            assert dock.assigned_forklifts == 0
    assert state.resources.assigned_workers <= state.resources.total_workers
    assert state.resources.assigned_forklifts <= state.resources.total_forklifts

    if state.last_recommendation is not None:
        assert runtime.recommendation_trigger_batch
        assert all(event.trigger_type in VALID_TRIGGER_TYPES for event in runtime.recommendation_trigger_batch)


@contextlib.contextmanager
def api_server_context(runtime: DashboardRuntime) -> Iterator[str]:
    handler = _build_handler(runtime, threading.Lock())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get_state_via_api(base_url: str) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url}/api/state", method="GET")
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def run_minutes_via_api(base_url: str, minutes: int) -> dict[str, Any]:
    return _post_json(base_url, "/api/step", {"minutes": minutes})


def apply_recommendation_via_api(base_url: str) -> dict[str, Any]:
    return _post_json(base_url, "/api/recommendation/apply", {})


def keep_plan_via_api(base_url: str) -> dict[str, Any]:
    return _post_json(base_url, "/api/recommendation/keep", {})


def update_supervisor_via_api(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _post_json(base_url, "/api/supervisor", payload)


def _post_json(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))

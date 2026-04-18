from __future__ import annotations

import random

import pytest

from tests.fixtures_dashboard import make_seeded_runtime, seed_yard_state
from yard.config import YardConfig
from yard.dashboard_runtime import DashboardRuntime
from yard.engine import initialize_state, snapshot_from_state
from yard.forecasting import build_forecast
from yard.models import Truck
from yard.verification import build_verification_bundle, ci_half_width_ratio, littles_law_check


def _make_runtime(config: YardConfig, *, seed: int) -> DashboardRuntime:
    state = initialize_state(
        available_workers=6,
        available_forklifts=2,
        active_docks=3,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    return DashboardRuntime(config=config, state=state, rng_seed=seed)


def _throughput_from_state(runtime: DashboardRuntime) -> float:
    now = runtime.state.now_minute
    effective_window = max(min(60, now + 1), 1)
    window_start = now - effective_window + 1
    departures = sum(
        1
        for truck in runtime.state.completed_trucks
        if truck.departure_minute is not None and window_start <= truck.departure_minute <= now
    )
    return departures * (60.0 / effective_window)


def _active_assignments(runtime: DashboardRuntime) -> tuple[dict[int, int], dict[int, int]]:
    workers = {dock_id: dock.assigned_workers for dock_id, dock in runtime.state.docks.items() if dock.active}
    forklifts = {dock_id: dock.assigned_forklifts for dock_id, dock in runtime.state.docks.items() if dock.active}
    return workers, forklifts


def test_dashboard_analytics_values_match_backend_calculations() -> None:
    config = YardConfig(
        arrival_rate_per_hour=20.0,
        review_interval_minutes=1,
        lookahead_horizon_minutes=12,
        evaluation_replications=3,
    )
    runtime = _make_runtime(config, seed=501)
    runtime.step(8)
    payload = runtime.get_dashboard_payload()
    snapshot = snapshot_from_state(runtime.state)
    forecast = build_forecast(runtime.state, config)

    assert payload["kpis"]["queue_length"] == runtime.state.queue_length
    assert payload["kpis"]["predicted_avg_wait_minutes"] == round(float(snapshot.predicted_avg_wait_minutes or 0.0), 2)
    assert payload["kpis"]["predicted_avg_time_in_system_minutes"] == round(
        float(snapshot.predicted_avg_time_in_system_minutes or 0.0),
        2,
    )
    assert payload["kpis"]["dock_utilization"] == round(100.0 * float(snapshot.predicted_dock_utilization or 0.0), 1)
    assert payload["kpis"]["staging_risk_pct"] == round(100.0 * float(snapshot.predicted_staging_overflow_risk or 0.0), 1)

    expected_throughput = _throughput_from_state(runtime)
    assert payload["kpis"]["throughput_trucks_per_hour"] == pytest.approx(round(expected_throughput, 2), abs=0.01)

    assert payload["forecast"]["baseline_rate_per_hour"] == pytest.approx(forecast.baseline_rate_per_hour, abs=1e-6)
    assert payload["forecast"]["smoothed_rate_per_hour"] == pytest.approx(forecast.smoothed_rate_per_hour, abs=1e-6)
    assert payload["forecast"]["expected_arrivals"] == pytest.approx(forecast.expected_arrivals, abs=1e-6)
    assert payload["forecast"]["scenarios"]["low"] == pytest.approx(forecast.scenarios["low"], abs=1e-6)
    assert payload["forecast"]["scenarios"]["baseline"] == pytest.approx(forecast.scenarios["baseline"], abs=1e-6)
    assert payload["forecast"]["scenarios"]["high"] == pytest.approx(forecast.scenarios["high"], abs=1e-6)

    assert payload["forecast"]["scenarios"]["low"] <= payload["forecast"]["scenarios"]["baseline"]
    assert payload["forecast"]["scenarios"]["baseline"] <= payload["forecast"]["scenarios"]["high"]

    if runtime.state.recent_replication_means:
        verification_expected = build_verification_bundle(
            arrival_rate_per_hour=float(snapshot.predicted_effective_flow_rate_per_hour or 0.0),
            avg_time_in_system_minutes=float(snapshot.predicted_avg_time_in_system_minutes or 0.0),
            avg_number_in_system=float(snapshot.predicted_avg_number_in_system or 0.0),
            replication_means=list(runtime.state.recent_replication_means),
            littles_law_threshold=config.verification_littles_law_threshold,
            ci_threshold=config.verification_ci_ratio_threshold,
        )
        assert payload["verification"]["spec_3"]["value"] == pytest.approx(
            verification_expected["spec_3_littles_law"]["relative_error"],
            abs=1e-6,
        )
        assert payload["verification"]["spec_4"]["value"] == pytest.approx(
            verification_expected["spec_4_ci_halfwidth"]["ratio"],
            abs=1e-6,
        )


def test_scenario_a_idle_low_load_behavior() -> None:
    config = YardConfig(
        arrival_rate_per_hour=2.0,
        review_interval_minutes=999,
        lookahead_horizon_minutes=20,
    )
    runtime = _make_runtime(config, seed=610)
    runtime.step(30)
    payload = runtime.get_dashboard_payload()

    assert payload["kpis"]["queue_length"] <= 3
    assert payload["kpis"]["predicted_avg_wait_minutes"] < 6.0
    assert payload["kpis"]["predicted_avg_time_in_system_minutes"] < 50.0
    assert payload["kpis"]["staging_risk_pct"] < 20.0
    assert payload["kpis"]["dock_utilization"] < 70.0


def test_scenario_b_growing_queue_pressure_behavior() -> None:
    config = YardConfig(
        arrival_rate_per_hour=36.0,
        review_interval_minutes=999,
        lookahead_horizon_minutes=20,
        floor_unload_worker_rate=0.35,
        pallet_unload_forklift_rate=0.45,
        clear_worker_rate=0.20,
        clear_forklift_rate=0.25,
    )
    state = initialize_state(
        available_workers=2,
        available_forklifts=1,
        active_docks=2,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    runtime = DashboardRuntime(config=config, state=state, rng_seed=611)
    queue_start = runtime.state.queue_length
    runtime.step(35)
    payload = runtime.get_dashboard_payload()

    assert payload["kpis"]["queue_length"] >= queue_start
    assert max(payload["trends"]["queue_length"]) >= payload["trends"]["queue_length"][0]
    assert payload["kpis"]["predicted_avg_wait_minutes"] > 8.0
    assert payload["kpis"]["dock_utilization"] >= 50.0


def test_scenario_c_staging_threshold_trigger_and_recommendation_consistency() -> None:
    runtime = make_seeded_runtime(
        seed=612,
        config_overrides={
            "review_interval_minutes": 999,
            "arrival_rate_per_hour": 10.0,
            "evaluation_replications": 2,
        },
    )
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "large_floor", "truck_remaining": 48, "staging": 94.0, "workers": 2, "forklifts": 0},
            {"dock_id": 2, "truck_type": "medium_floor", "truck_remaining": 20, "staging": 15.0, "workers": 2, "forklifts": 0},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 14, "staging": 10.0, "workers": 0, "forklifts": 1},
            {"dock_id": 4, "staging": 0.0, "workers": 0, "forklifts": 0},
        ],
        queue_rows=[
            {"truck_id": "C-Q1", "truck_type": "small_floor", "remaining": 16},
            {"truck_id": "C-Q2", "truck_type": "medium_floor", "remaining": 24},
        ],
    )

    saw_threshold = False
    payload = runtime.get_dashboard_payload()
    for _ in range(10):
        payload = runtime.step(1)
        if any(event.trigger_type == "staging_threshold" and event.dock_id == 1 for event in runtime.last_trigger_batch):
            saw_threshold = True
            break

    assert saw_threshold
    assert payload["recommendation"]["minute_generated"] is not None
    assert any(source.startswith("staging_threshold") for source in payload["recommendation"]["trigger_source"])
    assert payload["forecast"]["scenarios"]["low"] <= payload["forecast"]["scenarios"]["baseline"] <= payload["forecast"]["scenarios"]["high"]
    assert "spec_3" in payload["verification"] and "spec_4" in payload["verification"]


def test_scenario_d_recommendation_apply_updates_live_assignments_and_future_cycle() -> None:
    config = YardConfig(
        arrival_rate_per_hour=18.0,
        review_interval_minutes=1,
        lookahead_horizon_minutes=12,
        min_score_improvement_to_switch=0.0,
        evaluation_replications=3,
    )
    state = initialize_state(
        available_workers=6,
        available_forklifts=2,
        active_docks=3,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    state.docks[1].current_truck = Truck("D-1", "large_floor", 70.0, 60.0, 0, assigned_dock_id=1, unload_start_minute=0)
    state.docks[1].staging.occupancy_units = 70.0
    state.docks[1].assigned_workers = 1
    state.docks[2].current_truck = Truck("D-2", "small_floor", 30.0, 8.0, 0, assigned_dock_id=2, unload_start_minute=0)
    state.docks[2].staging.occupancy_units = 5.0
    state.docks[2].assigned_workers = 4
    state.docks[3].current_truck = Truck("D-3", "small_palletized", 24.0, 20.0, 0, assigned_dock_id=3, unload_start_minute=0)
    state.docks[3].staging.occupancy_units = 2.0
    state.docks[3].assigned_forklifts = 1
    state.update_resource_assignment_counters()
    runtime = DashboardRuntime(config=config, state=state, rng_seed=613)

    recommendation_payload = runtime.get_dashboard_payload()
    for minute_offset in range(1, 12):
        recommendation_payload = runtime.step(1)
        recommendation = runtime.state.last_recommendation
        if recommendation is None:
            continue
        if recommendation.selected_action.action_name != "keep_current_plan":
            break
    recommendation = runtime.state.last_recommendation
    assert recommendation is not None
    assert recommendation.selected_action.action_name != "keep_current_plan"

    before_workers, before_forks = _active_assignments(runtime)
    before_kpis = dict(recommendation_payload["kpis"])
    selected_action = recommendation.selected_action
    applied_payload = runtime.apply_recommendation()
    after_workers, after_forks = _active_assignments(runtime)

    assert runtime.state.active_action is not None
    assert runtime.state.active_action.action_name == selected_action.action_name
    assert (before_workers != after_workers) or (before_forks != after_forks)
    assert sum(after_workers.values()) <= runtime.state.resources.total_workers
    assert sum(after_forks.values()) <= runtime.state.resources.total_forklifts
    assert runtime.state.hold_gate_release == bool(selected_action.hold_gate_release)

    # Confirm the updated assignments continue into the next cycle for currently busy docks.
    busy_before_step = {
        dock_id
        for dock_id, dock in runtime.state.docks.items()
        if dock.active and dock.current_truck is not None
    }
    runtime.step(1)
    for dock_id in busy_before_step:
        dock = runtime.state.docks[dock_id]
        if dock.current_truck is not None:
            assert dock.assigned_workers == after_workers[dock_id]
            assert dock.assigned_forklifts == after_forks[dock_id]

    changed_metrics = [
        key
        for key in ("predicted_avg_wait_minutes", "predicted_avg_time_in_system_minutes", "dock_utilization", "staging_risk_pct")
        if abs(applied_payload["kpis"][key] - before_kpis[key]) > 0.001
    ]
    assert changed_metrics


def test_scenario_e_verification_formula_correctness() -> None:
    spec3 = littles_law_check(
        arrival_rate_per_hour=24.0,
        avg_time_in_system_minutes=30.0,
        avg_number_in_system=12.0,
        threshold=0.25,
    )
    expected_rhs = (24.0 / 60.0) * 30.0
    expected_error = abs(12.0 - expected_rhs) / max(12.0, expected_rhs, 1e-6)
    assert spec3["rhs_lambda_times_W"] == pytest.approx(expected_rhs, abs=1e-9)
    assert spec3["relative_error"] == pytest.approx(expected_error, abs=1e-9)
    assert spec3["pass"] is True

    replications = [20.0, 22.0, 24.0, 21.0, 23.0]
    spec4 = ci_half_width_ratio(replication_means=replications, threshold=0.30)
    mean = sum(replications) / len(replications)
    sample_var = sum((x - mean) ** 2 for x in replications) / (len(replications) - 1)
    sample_std = sample_var ** 0.5
    expected_half_width = 1.96 * sample_std / (len(replications) ** 0.5)
    expected_ratio = expected_half_width / max(abs(mean), 1e-6)
    assert spec4["mean"] == pytest.approx(mean, abs=1e-9)
    assert spec4["half_width"] == pytest.approx(expected_half_width, abs=1e-9)
    assert spec4["ratio"] == pytest.approx(expected_ratio, abs=1e-9)
    assert spec4["pass"] is True

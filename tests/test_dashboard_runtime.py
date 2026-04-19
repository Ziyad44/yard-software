from yard.config import YardConfig
from yard.dashboard_runtime import DashboardRuntime
from yard.engine import initialize_state
from yard.models import Truck


def _runtime(review_interval: int = 1) -> DashboardRuntime:
    config = YardConfig(
        arrival_rate_per_hour=8.0,
        review_interval_minutes=review_interval,
        lookahead_horizon_minutes=8,
    )
    state = initialize_state(
        available_workers=6,
        available_forklifts=2,
        active_docks=3,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    return DashboardRuntime(config=config, state=state, rng_seed=11)


def test_dashboard_payload_contains_required_sections() -> None:
    runtime = _runtime()
    payload = runtime.get_dashboard_payload()

    assert "live_operations" in payload
    assert "kpis" in payload
    assert "recommendation" in payload
    assert "forecast" in payload
    assert "ise_output" in payload
    assert "staging_status" in payload
    assert "dock_status" in payload
    assert "resource_summary" in payload
    assert "verification" in payload
    assert "queue_table" in payload
    assert "gate_history" in payload
    assert "trends" in payload
    assert payload["supervisor_inputs"]["available_workers"] == 6


def test_step_generates_recommendation_on_trigger() -> None:
    runtime = _runtime(review_interval=1)
    payload = runtime.step(minutes=1)

    assert payload["minute"] == 1
    assert payload["recommendation"]["minute_generated"] == 1
    assert payload["recommendation"]["is_applied"] is False
    assert payload["recommendation"]["trigger_source"]
    assert "selected_dock_reason" in payload["recommendation"]
    assert "resource_source_reason" in payload["recommendation"]
    assert "kpi_delta" in payload["recommendation"]
    assert "selection_note" in payload["recommendation"]
    top_candidates = payload["recommendation"]["top_candidates"]
    if top_candidates:
        ranks = [row["rank"] for row in top_candidates]
        assert all(rank >= 1 for rank in ranks)
        assert len(set(ranks)) == len(ranks)
        assert any(row["is_selected"] for row in top_candidates)
    assert len(payload["trends"]["minutes"]) >= 2


def test_apply_recommendation_and_keep_current_plan_update_decision() -> None:
    runtime = _runtime(review_interval=1)
    runtime.step(minutes=1)
    applied = runtime.apply_recommendation()
    assert applied["recommendation"]["is_applied"] is True
    assert applied["recommendation"]["decision_status"] == "applied"

    kept = runtime.keep_current_plan()
    assert kept["recommendation"]["is_applied"] is False
    assert kept["recommendation"]["decision_status"] == "kept_current_plan"


def test_update_supervisor_changes_controls_and_constraints() -> None:
    runtime = _runtime()
    payload = runtime.update_supervisor(
        {
            "available_workers": 4,
            "available_forklifts": 1,
            "active_docks": 2,
            "max_unloaders_per_dock": 2,
        }
    )
    supervisor = payload["supervisor_inputs"]

    assert supervisor["available_workers"] == 4
    assert supervisor["available_forklifts"] == 1
    assert supervisor["active_docks"] == 2
    assert supervisor["max_unloaders_per_dock"] == 2

    for row in payload["dock_status"]:
        assert row["assigned_workers"] <= 2


def test_idle_dock_has_zero_assignments_at_initial_payload() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0, review_interval_minutes=30)
    state = initialize_state(
        available_workers=5,
        available_forklifts=3,
        active_docks=4,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    dock4 = state.docks[4]
    dock4.current_truck = None
    dock4.staging.occupancy_units = 0.0
    dock4.assigned_workers = 2
    dock4.assigned_forklifts = 1
    state.update_resource_assignment_counters()

    runtime = DashboardRuntime(config=config, state=state, rng_seed=19)
    payload = runtime.get_dashboard_payload()

    row = next(item for item in payload["dock_status"] if item["dock_id"] == 4)
    assert row["status"] == "idle"
    assert row["assigned_workers"] == 0
    assert row["assigned_forklifts"] == 0
    assert runtime.state.docks[4].assigned_workers == 0
    assert runtime.state.docks[4].assigned_forklifts == 0


def test_idle_dock_returns_to_zero_assignments_after_becoming_idle() -> None:
    config = YardConfig(
        arrival_rate_per_hour=0.0,
        review_interval_minutes=999,
        floor_unload_worker_rate=4.0,
        
        clear_worker_rate=4.0,
        clear_forklift_rate=0.0,
    )
    state = initialize_state(
        available_workers=2,
        available_forklifts=0,
        active_docks=1,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    dock = state.docks[1]
    dock.current_truck = Truck(
        truck_id="IDLE-TEST-1",
        truck_type="small_floor",
        initial_load_units=2.0,
        remaining_load_units=2.0,
        gate_arrival_minute=0,
        assigned_dock_id=1,
        unload_start_minute=0,
    )
    dock.staging.occupancy_units = 0.0
    dock.assigned_workers = 1
    dock.assigned_forklifts = 0
    state.waiting_queue.clear()
    state.update_resource_assignment_counters()

    runtime = DashboardRuntime(config=config, state=state, rng_seed=29)
    payload = runtime.step(minutes=2)
    row = next(item for item in payload["dock_status"] if item["dock_id"] == 1)

    assert row["status"] == "idle"
    assert row["assigned_workers"] == 0
    assert row["assigned_forklifts"] == 0
    assert runtime.state.docks[1].assigned_workers == 0
    assert runtime.state.docks[1].assigned_forklifts == 0


def test_neutral_recommendation_payload_when_none_exists() -> None:
    runtime = _runtime(review_interval=999)
    payload = runtime.get_dashboard_payload()
    recommendation = payload["recommendation"]

    assert runtime.state.last_recommendation is None
    assert recommendation["text"] == "No active recommendation."
    assert recommendation["rationale"] == "Waiting for next trigger."
    assert recommendation["decision_status"] == "none"
    assert recommendation["is_applied"] is False
    assert recommendation["trigger_source"] == []
    assert recommendation["selected_target_dock_id"] is None
    assert recommendation["selected_dock_reason"] == ""
    assert recommendation["resource_source_reason"] == ""
    assert recommendation["kpi_delta"] == {}
    assert recommendation["selection_note"] == ""


def test_verification_cards_exist_and_survive_state_transitions() -> None:
    runtime = _runtime(review_interval=2)
    payload_initial = runtime.get_dashboard_payload()

    for key in ("spec_3", "spec_4"):
        assert key in payload_initial["verification"]
        card = payload_initial["verification"][key]
        assert "title" in card
        assert "status" in card
        assert "current_value" in card
        assert "target" in card

    runtime.step(minutes=4)
    payload_after_step = runtime.get_dashboard_payload()
    for key in ("spec_3", "spec_4"):
        assert key in payload_after_step["verification"]

    if runtime.state.last_recommendation is not None:
        payload_after_apply = runtime.apply_recommendation()
        for key in ("spec_3", "spec_4"):
            assert key in payload_after_apply["verification"]


def test_default_runtime_uses_fixed_staging_capacity() -> None:
    runtime = DashboardRuntime.create_default()
    payload = runtime.get_dashboard_payload()

    assert runtime.config.staging_capacity_units == 40.0
    assert all(dock.staging.capacity_units == 40.0 for dock in runtime.state.docks.values())
    assert all(row["staging_capacity_units"] == 40.0 for row in payload["dock_status"])


def test_supervisor_update_enforces_fixed_staging_capacity_for_existing_and_new_docks() -> None:
    runtime = DashboardRuntime.create_default()
    runtime.state.docks[1].staging.capacity_units = 100.0

    payload = runtime.update_supervisor({"active_docks": 5})

    assert runtime.state.docks[1].staging.capacity_units == 40.0
    assert runtime.state.docks[5].staging.capacity_units == 40.0
    assert all(row["staging_capacity_units"] == 40.0 for row in payload["dock_status"])


def test_palletized_step_conservation_matches_dashboard_row_values() -> None:
    config = YardConfig(
        arrival_rate_per_hour=0.0,
        review_interval_minutes=999,
        clear_forklift_rate=1.2,
    )
    state = initialize_state(
        available_workers=0,
        available_forklifts=1,
        active_docks=1,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    dock = state.docks[1]
    dock.current_truck = Truck(
        truck_id="UI-PALLET-1",
        truck_type="medium_palletized",
        initial_load_units=12.0,
        remaining_load_units=12.0,
        gate_arrival_minute=0,
        assigned_dock_id=1,
        unload_start_minute=0,
    )
    dock.assigned_workers = 0
    dock.assigned_forklifts = 1
    dock.staging.occupancy_units = 2.2
    dock.staging.load_family = "palletized"
    state.waiting_queue.clear()
    state.update_resource_assignment_counters()

    runtime = DashboardRuntime(config=config, state=state, rng_seed=17)
    remaining_before = dock.current_truck.remaining_load_units
    staging_before = dock.staging.occupancy_units

    payload = runtime.step(minutes=1)

    dock_after = runtime.state.docks[1]
    assert dock_after.current_truck is not None
    unloaded_this_step = remaining_before - dock_after.current_truck.remaining_load_units
    staging_delta = dock_after.staging.occupancy_units - staging_before
    assert abs(unloaded_this_step - staging_delta) < 1e-6

    row = next(item for item in payload["dock_status"] if item["dock_id"] == 1)
    assert row["staging_occupancy_units"] == round(dock_after.staging.occupancy_units, 1)
    assert row["remaining_load_units"] == round(dock_after.current_truck.remaining_load_units, 1)

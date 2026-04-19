import copy
import random

import pytest

from yard.config import YardConfig
from yard.dashboard_runtime import DashboardRuntime
from yard.engine import apply_action, initialize_state, run_minute_cycle
from yard.models import Action, DockState, Recommendation, StagingAreaState, Truck
from yard.simulation import dispatch_waiting_trucks, update_busy_dock_one_step


def test_state_transitions_enforce_strict_release_minute_by_minute() -> None:
    config = YardConfig(
        arrival_rate_per_hour=0.0,
        floor_unload_worker_rate=10.0,
        
        clear_worker_rate=1.0,
        clear_forklift_rate=0.0,
    )
    dock = DockState(
        dock_id=1,
        active=True,
        current_truck=Truck(
            truck_id="T10001",
            truck_type="small_floor",
            initial_load_units=12.0,
            remaining_load_units=12.0,
            gate_arrival_minute=0,
        ),
        assigned_workers=1,
        assigned_forklifts=0,
        staging=StagingAreaState(dock_id=1, occupancy_units=0.0, capacity_units=100.0),
    )

    assert not update_busy_dock_one_step(dock, config)
    assert dock.current_truck is not None
    assert dock.current_truck.remaining_load_units == pytest.approx(2.0)
    assert dock.staging.occupancy_units == pytest.approx(10.0)

    assert not update_busy_dock_one_step(dock, config)
    assert dock.current_truck is not None
    assert dock.current_truck.remaining_load_units == pytest.approx(0.0)
    assert dock.staging.occupancy_units == pytest.approx(11.0)

    for expected in range(10, 0, -1):
        assert not update_busy_dock_one_step(dock, config)
        assert dock.current_truck is not None
        assert dock.staging.occupancy_units == pytest.approx(float(expected))

    assert update_busy_dock_one_step(dock, config)
    assert dock.current_truck is None
    assert dock.staging.occupancy_units == pytest.approx(0.0)


def test_blocked_dock_does_not_take_next_truck() -> None:
    state = initialize_state(
        available_workers=2,
        available_forklifts=1,
        active_docks=1,
        max_unloaders_per_dock=4,
    )
    dock = state.docks[1]
    dock.current_truck = None
    dock.staging.occupancy_units = 8.0
    state.waiting_queue.append(
        Truck(
            truck_id="T10002",
            truck_type="small_floor",
            initial_load_units=30.0,
            remaining_load_units=30.0,
            gate_arrival_minute=0,
        )
    )

    dispatch_waiting_trucks(state, minute=1)

    assert state.queue_length == 1
    assert dock.current_truck is None


def test_dock_freed_trigger_and_next_assignment_timing() -> None:
    config = YardConfig(
        arrival_rate_per_hour=0.0,
        review_interval_minutes=999,
        floor_unload_worker_rate=4.0,
        
        clear_worker_rate=1.0,
        clear_forklift_rate=0.0,
    )
    state = initialize_state(
        available_workers=1,
        available_forklifts=0,
        active_docks=1,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    apply_action(
        state,
        Action(
            action_name="manual",
            workers_by_dock={1: 1},
            forklifts_by_dock={1: 0},
        ),
        config=config,
    )
    state.waiting_queue.extend(
        [
            Truck("T10003", "small_floor", 4.0, 4.0, 0),
            Truck("T10004", "small_floor", 4.0, 4.0, 0),
        ]
    )

    dock_freed_minutes: list[int] = []
    second_start_minute = None
    for _ in range(8):
        triggers, _ = run_minute_cycle(state, config=config, rng=random.Random(11))
        for event in triggers:
            if event.trigger_type == "dock_freed":
                dock_freed_minutes.append(event.minute)
        current = state.docks[1].current_truck
        if current is not None and current.truck_id == "T10004":
            second_start_minute = current.unload_start_minute
            break

    assert dock_freed_minutes
    assert len(dock_freed_minutes) == 1
    assert second_start_minute == dock_freed_minutes[0]


def test_recommendation_only_generated_on_trigger() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0, review_interval_minutes=999)
    state = initialize_state(
        available_workers=2,
        available_forklifts=1,
        active_docks=1,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )

    for _ in range(10):
        triggers, recommendation = run_minute_cycle(state, config=config, rng=random.Random(4))
        assert not triggers
        assert recommendation is None

    state.next_review_minute = state.now_minute + 1
    triggers, recommendation = run_minute_cycle(state, config=config, rng=random.Random(5))
    assert any(event.trigger_type == "review_timer" for event in triggers)
    assert recommendation is not None


def test_step_one_vs_run_fifteen_are_consistent() -> None:
    config = YardConfig(arrival_rate_per_hour=14.0, review_interval_minutes=4, lookahead_horizon_minutes=10)
    base_state = initialize_state(
        available_workers=6,
        available_forklifts=2,
        active_docks=3,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    runtime_a = DashboardRuntime(config=config, state=base_state, rng_seed=17)
    runtime_b = copy.deepcopy(runtime_a)

    payload_a = runtime_a.step(15)
    for _ in range(15):
        payload_b = runtime_b.step(1)

    assert payload_a["minute"] == payload_b["minute"]
    assert payload_a["kpis"] == payload_b["kpis"]
    assert payload_a["dock_status"] == payload_b["dock_status"]
    assert payload_a["staging_status"] == payload_b["staging_status"]
    assert payload_a["resource_summary"] == payload_b["resource_summary"]
    assert payload_a["trends"] == payload_b["trends"]


def test_apply_recommendation_recomputes_kpis_without_extra_step() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0, review_interval_minutes=999, lookahead_horizon_minutes=15)
    state = initialize_state(
        available_workers=4,
        available_forklifts=1,
        active_docks=2,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    runtime = DashboardRuntime(config=config, state=state, rng_seed=21)

    for idx in range(12):
        state.waiting_queue.append(
            Truck(
                truck_id=f"T2{idx:04d}",
                truck_type="small_floor",
                initial_load_units=30.0,
                remaining_load_units=30.0,
                gate_arrival_minute=0,
            )
        )

    before = runtime.get_dashboard_payload()["kpis"]["predicted_avg_wait_minutes"]
    workers_by_dock = {dock_id: dock.assigned_workers for dock_id, dock in state.docks.items() if dock.active}
    forklifts_by_dock = {
        dock_id: dock.assigned_forklifts for dock_id, dock in state.docks.items() if dock.active
    }
    state.last_recommendation = Recommendation(
        selected_action=Action(
            action_name="test_hold_gate",
            workers_by_dock=workers_by_dock,
            forklifts_by_dock=forklifts_by_dock,
            hold_gate_release=True,
            notes="Synthetic hold-gate recommendation for cache refresh validation.",
        ),
        rationale="Hold next gate release.",
        score=1.0,
        candidate_scores={"test_hold_gate": 1.0},
    )

    after_payload = runtime.apply_recommendation()
    after = after_payload["kpis"]["predicted_avg_wait_minutes"]

    assert state.hold_gate_release is True
    assert after > before


def test_keep_current_plan_requires_existing_recommendation() -> None:
    runtime = DashboardRuntime.create_default()
    with pytest.raises(ValueError):
        runtime.keep_current_plan()


def test_clearing_dock_is_not_reported_as_idle() -> None:
    runtime = DashboardRuntime.create_default()
    runtime.state.docks[1].current_truck = None
    runtime.state.docks[1].staging.occupancy_units = 16.0

    payload = runtime.get_dashboard_payload()
    row = next(item for item in payload["dock_status"] if item["dock_id"] == 1)

    assert row["status"] == "busy"
    assert row["phase"] == "clearing"


def test_busy_dock_does_not_freeze_when_active_dock_target_reduces() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0, review_interval_minutes=999)
    state = initialize_state(
        available_workers=4,
        available_forklifts=1,
        active_docks=2,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    runtime = DashboardRuntime(config=config, state=state, rng_seed=8)

    runtime.state.docks[2].current_truck = Truck(
        truck_id="T30001",
        truck_type="medium_floor",
        initial_load_units=50.0,
        remaining_load_units=20.0,
        gate_arrival_minute=0,
    )
    runtime.state.docks[2].staging.occupancy_units = 8.0
    runtime.state.docks[2].assigned_workers = 1
    runtime.state.docks[2].assigned_forklifts = 0
    runtime.state.update_resource_assignment_counters()

    runtime.update_supervisor({"active_docks": 1, "available_workers": 3, "available_forklifts": 1})
    before = runtime.state.docks[2].current_truck.remaining_load_units
    assert runtime.state.docks[2].active is True

    runtime.step(2)
    after = runtime.state.docks[2].current_truck.remaining_load_units

    assert after < before

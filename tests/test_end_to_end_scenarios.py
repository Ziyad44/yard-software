import copy

from yard.config import YardConfig
from yard.dashboard_runtime import DashboardRuntime
from yard.engine import apply_action, initialize_state
from yard.models import Action, Truck


def _runtime_from_config(
    config: YardConfig,
    *,
    workers: int,
    forklifts: int,
    docks: int,
    seed: int = 13,
) -> DashboardRuntime:
    state = initialize_state(
        available_workers=workers,
        available_forklifts=forklifts,
        active_docks=docks,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    return DashboardRuntime(config=config, state=state, rng_seed=seed)


def test_scenario_a_normal_flow() -> None:
    runtime = _runtime_from_config(
        YardConfig(arrival_rate_per_hour=10.0, review_interval_minutes=5, lookahead_horizon_minutes=12),
        workers=8,
        forklifts=3,
        docks=3,
    )

    runtime.step(90)
    payload = runtime.get_dashboard_payload()

    assert payload["minute"] == 90
    assert payload["recommendation"]["minute_generated"] is not None
    assert payload["resource_summary"]["workers_assigned"] <= payload["resource_summary"]["workers_total"]
    assert payload["resource_summary"]["forklifts_assigned"] <= payload["resource_summary"]["forklifts_total"]
    assert all(
        0.0 <= card["occupancy_units"] <= runtime.config.staging_capacity_units
        for card in payload["staging_status"]
    )
    assert len(payload["trends"]["minutes"]) == len(payload["trends"]["queue_length"])
    assert len(payload["trends"]["minutes"]) == len(payload["trends"]["arrivals"])


def test_scenario_b_staging_congestion_and_pause() -> None:
    config = YardConfig(
        arrival_rate_per_hour=0.0,
        review_interval_minutes=999,
        floor_unload_worker_rate=6.0,
        
        clear_worker_rate=0.0,
        clear_forklift_rate=0.0,
        staging_high_threshold=0.85,
        staging_low_threshold=0.75,
    )
    runtime = _runtime_from_config(config, workers=1, forklifts=0, docks=1)
    runtime.state.waiting_queue.append(
        Truck(
            truck_id="TCONGEST",
            truck_type="small_floor",
            initial_load_units=160.0,
            remaining_load_units=160.0,
            gate_arrival_minute=0,
        )
    )

    threshold_events = 0
    reached_full = False
    remaining_at_full = None
    capacity_limit = config.staging_capacity_units

    for _ in range(80):
        runtime.step(1)
        if any(event.trigger_type == "staging_threshold" for event in runtime.last_trigger_batch):
            threshold_events += 1

        dock = runtime.state.docks[1]
        if dock.staging.occupancy_units >= capacity_limit:
            reached_full = True
            remaining_at_full = dock.current_truck.remaining_load_units if dock.current_truck else None
            break

    assert reached_full
    assert threshold_events == 1

    runtime.step(3)
    dock_after = runtime.state.docks[1]
    assert dock_after.staging.occupancy_units == capacity_limit
    assert dock_after.current_truck is not None
    assert dock_after.current_truck.remaining_load_units == remaining_at_full


def test_scenario_c_dock_freed_then_next_truck_starts() -> None:
    config = YardConfig(
        arrival_rate_per_hour=0.0,
        review_interval_minutes=999,
        floor_unload_worker_rate=4.0,
        
        clear_worker_rate=1.0,
        clear_forklift_rate=0.0,
    )
    runtime = _runtime_from_config(config, workers=1, forklifts=0, docks=1)
    runtime.state.waiting_queue.extend(
        [
            Truck("TSCA", "small_floor", 4.0, 4.0, 0),
            Truck("TSCB", "small_floor", 4.0, 4.0, 0),
        ]
    )

    dock_freed_minute = None
    second_start = None
    for _ in range(10):
        runtime.step(1)
        if any(event.trigger_type == "dock_freed" for event in runtime.last_trigger_batch):
            dock_freed_minute = runtime.state.now_minute
        current = runtime.state.docks[1].current_truck
        if current is not None and current.truck_id == "TSCB":
            second_start = current.unload_start_minute
            break

    assert dock_freed_minute is not None
    assert second_start == dock_freed_minute


def test_scenario_d_recommendation_apply_changes_future_path() -> None:
    config = YardConfig(
        arrival_rate_per_hour=12.0,
        review_interval_minutes=999,
        lookahead_horizon_minutes=12,
        min_score_improvement_to_switch=0.0,
    )
    runtime = _runtime_from_config(config, workers=8, forklifts=3, docks=4, seed=41)
    apply_action(
        runtime.state,
        Action(
            action_name="manual_imbalance",
            workers_by_dock={1: 4, 2: 0, 3: 0, 4: 0},
            forklifts_by_dock={1: 3, 2: 0, 3: 0, 4: 0},
        ),
        config=config,
    )
    runtime.state.docks[2].staging.occupancy_units = runtime.config.staging_capacity_units * 0.96
    runtime.state.docks[2].staging.threshold_alert_active = False

    runtime.step(1)
    assert runtime.state.last_recommendation is not None
    selected_action_name = runtime.state.last_recommendation.selected_action.action_name
    apply_path = copy.deepcopy(runtime)
    keep_path = copy.deepcopy(runtime)

    apply_path.apply_recommendation()
    keep_path.keep_current_plan()

    for _ in range(12):
        apply_path.step(1)
        keep_path.step(1)

    apply_workers = [dock.assigned_workers for dock in apply_path.state.docks.values()]
    keep_workers = [dock.assigned_workers for dock in keep_path.state.docks.values()]
    apply_forks = [dock.assigned_forklifts for dock in apply_path.state.docks.values()]
    keep_forks = [dock.assigned_forklifts for dock in keep_path.state.docks.values()]

    if selected_action_name == "keep_current_plan":
        assert apply_workers == keep_workers
        assert apply_forks == keep_forks
        assert apply_path.state.docks[2].staging.occupancy_units == keep_path.state.docks[2].staging.occupancy_units
    else:
        assert apply_workers != keep_workers or apply_forks != keep_forks
        assert apply_path.state.docks[2].staging.occupancy_units != keep_path.state.docks[2].staging.occupancy_units


def test_scenario_e_supervisor_constraint_change_rebalances_safely() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0, review_interval_minutes=999)
    runtime = _runtime_from_config(config, workers=6, forklifts=2, docks=3)
    runtime.state.docks[3].current_truck = Truck(
        truck_id="TSUP1",
        truck_type="medium_floor",
        initial_load_units=50.0,
        remaining_load_units=25.0,
        gate_arrival_minute=0,
    )
    runtime.state.docks[3].staging.occupancy_units = 6.0
    runtime.state.docks[3].assigned_workers = 1
    runtime.state.docks[3].assigned_forklifts = 0
    runtime.state.update_resource_assignment_counters()

    payload = runtime.update_supervisor(
        {
            "available_workers": 3,
            "available_forklifts": 1,
            "active_docks": 1,
            "max_unloaders_per_dock": 2,
        }
    )

    assert payload["resource_summary"]["workers_assigned"] <= payload["resource_summary"]["workers_total"]
    assert payload["resource_summary"]["forklifts_assigned"] <= payload["resource_summary"]["forklifts_total"]
    assert all(row["assigned_workers"] <= 2 for row in payload["dock_status"])

    before = runtime.state.docks[3].current_truck.remaining_load_units
    runtime.step(2)
    after = runtime.state.docks[3].current_truck.remaining_load_units
    assert after < before

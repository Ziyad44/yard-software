from __future__ import annotations

import copy

from yard.models import Truck

from tests.fixtures_dashboard import (
    assert_dashboard_matches_backend,
    make_seeded_runtime,
    run_minutes_via_runtime,
    seed_yard_state,
    step_minutes_via_runtime,
)


def _assignments(runtime) -> tuple[dict[int, int], dict[int, int]]:
    workers = {dock_id: dock.assigned_workers for dock_id, dock in runtime.state.docks.items() if dock.active}
    forklifts = {dock_id: dock.assigned_forklifts for dock_id, dock in runtime.state.docks.items() if dock.active}
    return workers, forklifts


def test_scenario_healthy_balanced_flow_runtime() -> None:
    runtime = make_seeded_runtime(seed=101)
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 30, "staging": 18, "workers": 2, "forklifts": 1},
            {"dock_id": 2, "truck_type": "small_palletized", "truck_remaining": 18, "staging": 10, "workers": 0, "forklifts": 1},
            {"dock_id": 3, "truck_type": "medium_floor", "truck_remaining": 22, "staging": 14, "workers": 2, "forklifts": 0},
            {"dock_id": 4, "staging": 0, "workers": 1, "forklifts": 1},
        ],
        queue_rows=[
            {"truck_id": "Q001", "truck_type": "small_floor", "remaining": 20},
            {"truck_id": "Q002", "truck_type": "medium_palletized", "remaining": 24},
        ],
    )
    assert runtime.state.docks[4].can_accept_next_truck()
    before_queue = runtime.state.queue_length
    before_staging = {dock_id: dock.staging.occupancy_units for dock_id, dock in runtime.state.docks.items()}

    payload = run_minutes_via_runtime(runtime, 1)
    assert payload["kpis"]["queue_length"] == before_queue - 1
    assert all(event.trigger_type != "staging_threshold" for event in runtime.last_trigger_batch)
    after_staging = {dock_id: dock.staging.occupancy_units for dock_id, dock in runtime.state.docks.items()}
    assert any(abs(after_staging[dock_id] - before_staging[dock_id]) > 0.01 for dock_id in before_staging)
    assert payload["recommendation"]["minute_generated"] is None
    assert_dashboard_matches_backend(runtime, payload)


def test_scenario_strict_dock_release_runtime() -> None:
    runtime = make_seeded_runtime(seed=102, config_overrides={"review_interval_minutes": 999})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 2, "staging": 28, "workers": 1, "forklifts": 1},
            {"dock_id": 2, "truck_type": "large_floor", "truck_remaining": 25, "staging": 20, "workers": 2, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 15, "staging": 10, "workers": 0, "forklifts": 1},
            {"dock_id": 4, "truck_type": "medium_floor", "truck_remaining": 16, "staging": 12, "workers": 2, "forklifts": 0},
        ],
        queue_rows=[{"truck_id": "Q003", "truck_type": "small_floor", "remaining": 22}],
    )
    saw_zero_with_staging = False
    dock_freed_minute = None
    queue_drop_minute = None
    dock_freed_count = 0

    for _ in range(40):
        pre_queue = runtime.state.queue_length
        payload = run_minutes_via_runtime(runtime, 1)
        post_queue = runtime.state.queue_length
        dock1 = runtime.state.docks[1]

        if dock1.current_truck is not None and dock1.current_truck.remaining_load_units <= 1e-6 and dock1.staging.occupancy_units > 0.0:
            saw_zero_with_staging = True
            assert pre_queue == 1 and post_queue == 1
            row = next(row for row in payload["dock_status"] if row["dock_id"] == 1)
            assert row["status"] == "busy"

        if pre_queue == 1 and post_queue == 0 and queue_drop_minute is None:
            queue_drop_minute = runtime.state.now_minute

        for event in runtime.last_trigger_batch:
            if event.trigger_type == "dock_freed" and event.dock_id == 1:
                dock_freed_count += 1
                dock_freed_minute = event.minute
        if queue_drop_minute is not None and dock_freed_minute is not None:
            break

    assert saw_zero_with_staging
    assert dock_freed_count == 1
    assert dock_freed_minute is not None
    assert queue_drop_minute == dock_freed_minute


def test_scenario_staging_congestion_pause_runtime() -> None:
    runtime = make_seeded_runtime(
        seed=103,
        config_overrides={
            "review_interval_minutes": 999,
            "clear_worker_rate": 0.0,
            "clear_forklift_rate": 0.0,
        },
    )
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "large_floor", "truck_remaining": 40, "staging": 94, "workers": 2, "forklifts": 0},
            {"dock_id": 2, "truck_type": "medium_floor", "truck_remaining": 20, "staging": 25, "workers": 2, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 12, "staging": 8, "workers": 0, "forklifts": 1},
            {"dock_id": 4, "staging": 0, "workers": 1, "forklifts": 1},
        ],
        queue_rows=[
            {"truck_id": "Q004", "truck_type": "small_floor", "remaining": 15},
            {"truck_id": "Q005", "truck_type": "medium_floor", "remaining": 30},
            {"truck_id": "Q006", "truck_type": "small_palletized", "remaining": 18},
        ],
    )

    threshold_events = 0
    reached_full = False
    remaining_at_full = None
    for _ in range(25):
        payload = run_minutes_via_runtime(runtime, 1)
        if any(event.trigger_type == "staging_threshold" and event.dock_id == 1 for event in runtime.last_trigger_batch):
            threshold_events += 1
        dock1 = runtime.state.docks[1]
        if dock1.staging.occupancy_units >= 100.0:
            reached_full = True
            remaining_at_full = dock1.current_truck.remaining_load_units if dock1.current_truck else None
            card = next(card for card in payload["staging_status"] if card["dock_id"] == 1)
            assert card["traffic_light"] == "red"
            break

    assert reached_full
    assert threshold_events == 1
    assert runtime.state.last_recommendation is not None

    step_minutes_via_runtime(runtime, 3)
    dock1_after = runtime.state.docks[1]
    assert dock1_after.staging.occupancy_units == 100.0
    assert dock1_after.current_truck is not None
    assert dock1_after.current_truck.remaining_load_units == remaining_at_full


def test_scenario_apply_recommendation_changes_future_runtime() -> None:
    runtime = make_seeded_runtime(seed=104, config_overrides={"min_score_improvement_to_switch": 0.0})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 25, "staging": 10, "workers": 4, "forklifts": 2},
            {"dock_id": 2, "truck_type": "large_floor", "truck_remaining": 35, "staging": 90, "workers": 0, "forklifts": 0},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 12, "staging": 8, "workers": 1, "forklifts": 1},
            {"dock_id": 4, "staging": 0, "workers": 0, "forklifts": 0},
        ],
        queue_rows=[
            {"truck_id": "Q007", "truck_type": "medium_floor", "remaining": 24},
            {"truck_id": "Q008", "truck_type": "small_floor", "remaining": 18},
        ],
    )
    run_minutes_via_runtime(runtime, 1)
    assert runtime.state.last_recommendation is not None

    apply_branch = copy.deepcopy(runtime)
    keep_branch = copy.deepcopy(runtime)

    applied_payload = apply_branch.apply_recommendation()
    assert_dashboard_matches_backend(apply_branch, applied_payload)
    assert applied_payload["recommendation"]["decision_status"] == "applied"
    assert applied_payload["recommendation"]["is_applied"] is True
    kept_payload = keep_branch.keep_current_plan()
    assert_dashboard_matches_backend(keep_branch, kept_payload)

    apply_branch.step(12)
    keep_branch.step(12)

    apply_workers, apply_forks = _assignments(apply_branch)
    keep_workers, keep_forks = _assignments(keep_branch)
    assert apply_workers != keep_workers or apply_forks != keep_forks
    assert kept_payload["recommendation"]["decision_status"] == "kept_current_plan"
    assert kept_payload["recommendation"]["is_applied"] is False

    payload_apply = apply_branch.get_dashboard_payload()
    payload_keep = keep_branch.get_dashboard_payload()
    assert payload_apply["kpis"] != payload_keep["kpis"]
    assert payload_apply["trends"] != payload_keep["trends"]
    assert_dashboard_matches_backend(apply_branch, payload_apply)
    assert_dashboard_matches_backend(keep_branch, payload_keep)


def test_scenario_keep_current_plan_runtime() -> None:
    runtime = make_seeded_runtime(seed=105, config_overrides={"min_score_improvement_to_switch": 0.0})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 24, "staging": 12, "workers": 4, "forklifts": 2},
            {"dock_id": 2, "truck_type": "large_floor", "truck_remaining": 30, "staging": 88, "workers": 0, "forklifts": 0},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 14, "staging": 10, "workers": 1, "forklifts": 1},
            {"dock_id": 4, "staging": 0, "workers": 0, "forklifts": 0},
        ],
        queue_rows=[{"truck_id": "Q009", "truck_type": "small_floor", "remaining": 16}],
    )
    run_minutes_via_runtime(runtime, 1)
    assert runtime.state.last_recommendation is not None
    before_workers, before_forks = _assignments(runtime)
    kept_payload = runtime.keep_current_plan()
    after_workers, after_forks = _assignments(runtime)

    assert kept_payload["recommendation"]["decision_status"] == "kept_current_plan"
    assert kept_payload["recommendation"]["is_applied"] is False
    assert before_workers == after_workers
    assert before_forks == after_forks
    step_minutes_via_runtime(runtime, 5)
    final_workers, final_forks = _assignments(runtime)
    assert final_workers == after_workers
    assert final_forks == after_forks


def test_scenario_queue_buildup_runtime() -> None:
    runtime = make_seeded_runtime(
        seed=106,
        config_overrides={
            "arrival_rate_per_hour": 36.0,
            "clear_worker_rate": 0.2,
            "clear_forklift_rate": 0.2,
        },
    )
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "large_floor", "truck_remaining": 50, "staging": 35, "workers": 2, "forklifts": 1},
            {"dock_id": 2, "truck_type": "medium_floor", "truck_remaining": 42, "staging": 32, "workers": 1, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 26, "staging": 22, "workers": 1, "forklifts": 1},
            {"dock_id": 4, "truck_type": "medium_floor", "truck_remaining": 36, "staging": 28, "workers": 1, "forklifts": 0},
        ],
        queue_rows=[{"truck_id": "Q010", "truck_type": "small_floor", "remaining": 20}],
    )

    initial_queue = runtime.state.queue_length
    step_minutes_via_runtime(runtime, 35)
    payload = runtime.get_dashboard_payload()
    assert payload["kpis"]["queue_length"] >= initial_queue
    assert max(payload["trends"]["queue_length"]) >= initial_queue
    assert payload["kpis"]["dock_utilization"] >= 50.0
    if runtime.state.last_recommendation is not None:
        assert runtime.recommendation_trigger_batch


def test_scenario_independent_dock_behavior_runtime() -> None:
    runtime = make_seeded_runtime(seed=107)
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "large_floor", "truck_remaining": 30, "staging": 88, "workers": 2, "forklifts": 0},
            {"dock_id": 2, "staging": 40, "workers": 1, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 18, "staging": 12, "workers": 0, "forklifts": 1},
            {"dock_id": 4, "staging": 0, "workers": 2, "forklifts": 1},
        ],
        queue_rows=[
            {"truck_id": "Q011", "truck_type": "small_floor", "remaining": 14},
            {"truck_id": "Q012", "truck_type": "medium_palletized", "remaining": 20},
            {"truck_id": "Q013", "truck_type": "medium_floor", "remaining": 28},
        ],
    )

    occupancy_history: dict[int, list[float]] = {1: [], 2: [], 3: [], 4: []}
    dock4_received = False
    for _ in range(8):
        run_minutes_via_runtime(runtime, 1)
        for dock_id in occupancy_history:
            occupancy_history[dock_id].append(runtime.state.docks[dock_id].staging.occupancy_units)
        dock4 = runtime.state.docks[4]
        if dock4.current_truck is not None:
            dock4_received = True

    assert dock4_received
    assert occupancy_history[1][-1] != occupancy_history[2][-1]
    assert occupancy_history[2][-1] < occupancy_history[2][0]
    assert occupancy_history[3][-1] != occupancy_history[1][-1]


def test_scenario_review_timer_trigger_runtime() -> None:
    runtime = make_seeded_runtime(
        seed=108,
        config_overrides={
            "arrival_rate_per_hour": 0.0,
            "review_interval_minutes": 4,
            "floor_unload_worker_rate": 0.5,
            "floor_unload_forklift_assist_rate": 0.0,
            "pallet_unload_forklift_rate": 0.6,
            "clear_worker_rate": 1.0,
            "clear_forklift_rate": 1.0,
        },
    )
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "large_floor", "truck_remaining": 60, "staging": 5, "workers": 2, "forklifts": 1},
            {"dock_id": 2, "truck_type": "medium_floor", "truck_remaining": 50, "staging": 3, "workers": 1, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 40, "staging": 2, "workers": 1, "forklifts": 1},
            {"dock_id": 4, "truck_type": "medium_floor", "truck_remaining": 55, "staging": 4, "workers": 1, "forklifts": 0},
        ],
        queue_rows=[],
    )

    for _ in range(3):
        payload = run_minutes_via_runtime(runtime, 1)
        assert payload["recommendation"]["minute_generated"] is None
        assert all(event.trigger_type != "review_timer" for event in runtime.last_trigger_batch)

    payload = run_minutes_via_runtime(runtime, 1)
    assert any(event.trigger_type == "review_timer" for event in runtime.last_trigger_batch)
    assert payload["recommendation"]["minute_generated"] == runtime.state.now_minute
    assert "review_timer" in payload["recommendation"]["trigger_source"]


def test_scenario_supervisor_constraint_change_runtime() -> None:
    runtime = make_seeded_runtime(seed=109, config_overrides={"arrival_rate_per_hour": 8.0})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 28, "staging": 12, "workers": 2, "forklifts": 1},
            {"dock_id": 2, "truck_type": "large_floor", "truck_remaining": 30, "staging": 18, "workers": 1, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 16, "staging": 10, "workers": 1, "forklifts": 1},
            {"dock_id": 4, "staging": 0, "workers": 1, "forklifts": 0},
        ],
        queue_rows=[{"truck_id": "Q014", "truck_type": "small_floor", "remaining": 18}],
    )
    step_minutes_via_runtime(runtime, 3)
    payload = runtime.update_supervisor({"available_workers": 4, "available_forklifts": 2, "active_docks": 4, "max_unloaders_per_dock": 4})
    assert_dashboard_matches_backend(runtime, payload)
    assert payload["resource_summary"]["workers_total"] == 4
    assert payload["resource_summary"]["forklifts_total"] == 2
    assert payload["resource_summary"]["workers_assigned"] <= 4
    assert payload["resource_summary"]["forklifts_assigned"] <= 2
    step_minutes_via_runtime(runtime, 6)
    assert runtime.state.resources.assigned_workers <= runtime.state.resources.total_workers
    assert runtime.state.resources.assigned_forklifts <= runtime.state.resources.total_forklifts


def test_scenario_run15_matches_step15_runtime() -> None:
    runtime_a = make_seeded_runtime(seed=110, config_overrides={"arrival_rate_per_hour": 18.0, "review_interval_minutes": 5})
    seed_yard_state(
        runtime_a,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 32, "staging": 16, "workers": 2, "forklifts": 1},
            {"dock_id": 2, "truck_type": "small_palletized", "truck_remaining": 20, "staging": 10, "workers": 0, "forklifts": 1},
            {"dock_id": 3, "truck_type": "medium_floor", "truck_remaining": 25, "staging": 14, "workers": 2, "forklifts": 0},
            {"dock_id": 4, "staging": 0, "workers": 1, "forklifts": 1},
        ],
        queue_rows=[
            {"truck_id": "Q015", "truck_type": "small_floor", "remaining": 18},
            {"truck_id": "Q016", "truck_type": "medium_floor", "remaining": 24},
        ],
    )
    runtime_b = copy.deepcopy(runtime_a)

    payload_run = run_minutes_via_runtime(runtime_a, 15)
    for _ in range(15):
        payload_step = run_minutes_via_runtime(runtime_b, 1)

    assert payload_run["kpis"] == payload_step["kpis"]
    assert payload_run["dock_status"] == payload_step["dock_status"]
    assert payload_run["staging_status"] == payload_step["staging_status"]
    assert payload_run["resource_summary"] == payload_step["resource_summary"]
    assert payload_run["recommendation"] == payload_step["recommendation"]
    assert payload_run["trends"] == payload_step["trends"]


def test_scenario_simulated_arrivals_visible_runtime() -> None:
    runtime = make_seeded_runtime(seed=111, config_overrides={"arrival_rate_per_hour": 26.0, "review_interval_minutes": 5})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "large_floor", "truck_remaining": 45, "staging": 20, "workers": 2, "forklifts": 1},
            {"dock_id": 2, "truck_type": "medium_floor", "truck_remaining": 38, "staging": 18, "workers": 1, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 22, "staging": 12, "workers": 1, "forklifts": 1},
            {"dock_id": 4, "truck_type": "medium_floor", "truck_remaining": 30, "staging": 16, "workers": 1, "forklifts": 0},
        ],
        queue_rows=[{"truck_id": "Q017", "truck_type": "small_floor", "remaining": 14}],
    )

    step_minutes_via_runtime(runtime, 25)
    payload = runtime.get_dashboard_payload()
    arrivals_sum = sum(payload["trends"]["arrivals"])
    assert arrivals_sum > 0
    assert max(payload["trends"]["queue_length"]) >= payload["trends"]["queue_length"][0]
    assert payload["kpis"]["queue_length"] == runtime.state.queue_length


def test_scenario_full_end_to_end_dashboard_truth_runtime() -> None:
    runtime = make_seeded_runtime(seed=112, config_overrides={"arrival_rate_per_hour": 14.0, "review_interval_minutes": 5, "min_score_improvement_to_switch": 0.0})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "large_floor", "truck_remaining": 36, "staging": 84.9, "workers": 4, "forklifts": 2},
            {"dock_id": 2, "truck_type": "medium_floor", "truck_remaining": 2, "staging": 20, "workers": 1, "forklifts": 1},
            {"dock_id": 3, "staging": 0, "workers": 0, "forklifts": 0},
            {"dock_id": 4, "truck_type": "small_palletized", "truck_remaining": 16, "staging": 8, "workers": 0, "forklifts": 0},
        ],
        queue_rows=[
            {"truck_id": "Q018", "truck_type": "small_floor", "remaining": 12},
            {"truck_id": "Q019", "truck_type": "medium_floor", "remaining": 20},
            {"truck_id": "Q020", "truck_type": "small_palletized", "remaining": 15},
        ],
    )

    dock3_got_truck = False
    dock2_blocked_after_unload = False
    saw_threshold = False
    saw_recommendation = False

    for _ in range(20):
        payload = run_minutes_via_runtime(runtime, 1)
        dock3 = runtime.state.docks[3]
        if dock3.current_truck is not None:
            dock3_got_truck = True
        dock2 = runtime.state.docks[2]
        if dock2.current_truck is not None and dock2.current_truck.remaining_load_units <= 1e-6 and dock2.staging.occupancy_units > 0.0:
            dock2_blocked_after_unload = True
        if any(event.trigger_type == "staging_threshold" and event.dock_id == 1 for event in runtime.last_trigger_batch):
            saw_threshold = True
        if payload["recommendation"]["minute_generated"] is not None:
            saw_recommendation = True

    assert dock3_got_truck
    assert dock2_blocked_after_unload
    assert saw_threshold
    assert saw_recommendation
    assert runtime.state.last_recommendation is not None
    if runtime.state.last_recommendation.selected_action.action_name == "keep_current_plan":
        dock1 = runtime.state.docks[1]
        dock2 = runtime.state.docks[2]
        dock1.assigned_workers = 4
        dock1.assigned_forklifts = 2
        dock2.assigned_workers = 0
        dock2.assigned_forklifts = 0
        dock2.staging.occupancy_units = 95.0
        dock2.staging.threshold_alert_active = False
        if dock2.current_truck is None:
            dock2.current_truck = Truck(
                truck_id="QX-FULL",
                truck_type="large_floor",
                initial_load_units=50.0,
                remaining_load_units=35.0,
                gate_arrival_minute=runtime.state.now_minute,
                assigned_dock_id=2,
                unload_start_minute=runtime.state.now_minute,
            )
        runtime.state.update_resource_assignment_counters()
        run_minutes_via_runtime(runtime, 1)
    assert runtime.state.last_recommendation is not None
    assert runtime.state.last_recommendation.selected_action.action_name != "keep_current_plan"

    before_apply_workers, before_apply_forks = _assignments(runtime)
    apply_payload = runtime.apply_recommendation()
    assert_dashboard_matches_backend(runtime, apply_payload)
    after_apply_workers, after_apply_forks = _assignments(runtime)
    assert (before_apply_workers != after_apply_workers) or (before_apply_forks != after_apply_forks)

    step_minutes_via_runtime(runtime, 6)
    final_payload = runtime.get_dashboard_payload()
    assert_dashboard_matches_backend(runtime, final_payload)
    assert sum(final_payload["trends"]["arrivals"]) > 0

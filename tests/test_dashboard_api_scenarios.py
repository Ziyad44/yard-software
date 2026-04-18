from __future__ import annotations

import copy

from yard.models import Truck

from tests.fixtures_dashboard import (
    api_server_context,
    apply_recommendation_via_api,
    assert_dashboard_matches_backend,
    get_state_via_api,
    keep_plan_via_api,
    make_seeded_runtime,
    run_minutes_via_api,
    seed_yard_state,
    update_supervisor_via_api,
)


def _assignments(runtime) -> tuple[dict[int, int], dict[int, int]]:
    workers = {dock_id: dock.assigned_workers for dock_id, dock in runtime.state.docks.items() if dock.active}
    forklifts = {dock_id: dock.assigned_forklifts for dock_id, dock in runtime.state.docks.items() if dock.active}
    return workers, forklifts


def test_scenario_healthy_balanced_flow_api() -> None:
    runtime = make_seeded_runtime(seed=201)
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 30, "staging": 18, "workers": 2, "forklifts": 1},
            {"dock_id": 2, "truck_type": "small_palletized", "truck_remaining": 18, "staging": 10, "workers": 0, "forklifts": 1},
            {"dock_id": 3, "truck_type": "medium_floor", "truck_remaining": 22, "staging": 14, "workers": 2, "forklifts": 0},
            {"dock_id": 4, "staging": 0, "workers": 1, "forklifts": 1},
        ],
        queue_rows=[
            {"truck_id": "AQ001", "truck_type": "small_floor", "remaining": 20},
            {"truck_id": "AQ002", "truck_type": "medium_palletized", "remaining": 24},
        ],
    )
    with api_server_context(runtime) as base_url:
        before = get_state_via_api(base_url)
        assert_dashboard_matches_backend(runtime, before)
        assert runtime.state.docks[4].can_accept_next_truck()
        payload = run_minutes_via_api(base_url, 1)
        assert_dashboard_matches_backend(runtime, payload)
        assert payload["kpis"]["queue_length"] == before["kpis"]["queue_length"] - 1
        assert payload["recommendation"]["minute_generated"] is None


def test_scenario_strict_dock_release_api() -> None:
    runtime = make_seeded_runtime(seed=202, config_overrides={"review_interval_minutes": 999})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 2, "staging": 28, "workers": 1, "forklifts": 1},
            {"dock_id": 2, "truck_type": "large_floor", "truck_remaining": 25, "staging": 20, "workers": 2, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 15, "staging": 10, "workers": 0, "forklifts": 1},
            {"dock_id": 4, "truck_type": "medium_floor", "truck_remaining": 16, "staging": 12, "workers": 2, "forklifts": 0},
        ],
        queue_rows=[{"truck_id": "AQ003", "truck_type": "small_floor", "remaining": 22}],
    )
    with api_server_context(runtime) as base_url:
        saw_zero_with_staging = False
        dock_freed_minute = None
        queue_drop_minute = None
        for _ in range(40):
            pre = get_state_via_api(base_url)
            post = run_minutes_via_api(base_url, 1)
            assert_dashboard_matches_backend(runtime, post)
            dock1 = runtime.state.docks[1]
            if dock1.current_truck is not None and dock1.current_truck.remaining_load_units <= 1e-6 and dock1.staging.occupancy_units > 0.0:
                saw_zero_with_staging = True
                row = next(row for row in post["dock_status"] if row["dock_id"] == 1)
                assert row["status"] == "busy"
            if pre["kpis"]["queue_length"] == 1 and post["kpis"]["queue_length"] == 0 and queue_drop_minute is None:
                queue_drop_minute = post["minute"]
            if any(event.trigger_type == "dock_freed" and event.dock_id == 1 for event in runtime.last_trigger_batch):
                dock_freed_minute = post["minute"]
            if queue_drop_minute is not None and dock_freed_minute is not None:
                break

        assert saw_zero_with_staging
        assert dock_freed_minute == queue_drop_minute


def test_scenario_staging_congestion_pause_api() -> None:
    runtime = make_seeded_runtime(
        seed=203,
        config_overrides={"review_interval_minutes": 999, "clear_worker_rate": 0.0, "clear_forklift_rate": 0.0},
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
            {"truck_id": "AQ004", "truck_type": "small_floor", "remaining": 15},
            {"truck_id": "AQ005", "truck_type": "medium_floor", "remaining": 30},
            {"truck_id": "AQ006", "truck_type": "small_palletized", "remaining": 18},
        ],
    )
    with api_server_context(runtime) as base_url:
        threshold_events = 0
        remaining_at_full = None
        for _ in range(25):
            payload = run_minutes_via_api(base_url, 1)
            assert_dashboard_matches_backend(runtime, payload)
            if any(event.trigger_type == "staging_threshold" and event.dock_id == 1 for event in runtime.last_trigger_batch):
                threshold_events += 1
            dock1 = runtime.state.docks[1]
            if dock1.staging.occupancy_units >= 100.0:
                remaining_at_full = dock1.current_truck.remaining_load_units if dock1.current_truck else None
                card = next(card for card in payload["staging_status"] if card["dock_id"] == 1)
                assert card["traffic_light"] == "red"
                break
        assert threshold_events == 1
        assert remaining_at_full is not None
        run_minutes_via_api(base_url, 3)
        assert runtime.state.docks[1].current_truck.remaining_load_units == remaining_at_full


def test_scenario_apply_recommendation_changes_future_api() -> None:
    runtime = make_seeded_runtime(seed=204, config_overrides={"min_score_improvement_to_switch": 0.0})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 25, "staging": 10, "workers": 4, "forklifts": 2},
            {"dock_id": 2, "truck_type": "large_floor", "truck_remaining": 35, "staging": 90, "workers": 0, "forklifts": 0},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 12, "staging": 8, "workers": 1, "forklifts": 1},
            {"dock_id": 4, "staging": 0, "workers": 0, "forklifts": 0},
        ],
        queue_rows=[
            {"truck_id": "AQ007", "truck_type": "medium_floor", "remaining": 24},
            {"truck_id": "AQ008", "truck_type": "small_floor", "remaining": 18},
        ],
    )
    with api_server_context(runtime) as base_url:
        run_minutes_via_api(base_url, 1)
        assert runtime.state.last_recommendation is not None

        apply_branch = copy.deepcopy(runtime)
        keep_branch = copy.deepcopy(runtime)
        with api_server_context(apply_branch) as apply_url, api_server_context(keep_branch) as keep_url:
            apply_payload = apply_recommendation_via_api(apply_url)
            keep_payload = keep_plan_via_api(keep_url)
            assert_dashboard_matches_backend(apply_branch, apply_payload)
            assert apply_payload["recommendation"]["decision_status"] == "applied"
            assert apply_payload["recommendation"]["is_applied"] is True
            assert_dashboard_matches_backend(keep_branch, keep_payload)
            run_minutes_via_api(apply_url, 12)
            run_minutes_via_api(keep_url, 12)

            apply_workers, apply_forks = _assignments(apply_branch)
            keep_workers, keep_forks = _assignments(keep_branch)
            assert apply_workers != keep_workers or apply_forks != keep_forks
            assert keep_payload["recommendation"]["decision_status"] == "kept_current_plan"
            assert keep_payload["recommendation"]["is_applied"] is False
            assert apply_branch.get_dashboard_payload()["kpis"] != keep_branch.get_dashboard_payload()["kpis"]


def test_scenario_keep_current_plan_api() -> None:
    runtime = make_seeded_runtime(seed=205, config_overrides={"min_score_improvement_to_switch": 0.0})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 24, "staging": 12, "workers": 4, "forklifts": 2},
            {"dock_id": 2, "truck_type": "large_floor", "truck_remaining": 30, "staging": 88, "workers": 0, "forklifts": 0},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 14, "staging": 10, "workers": 1, "forklifts": 1},
            {"dock_id": 4, "staging": 0, "workers": 0, "forklifts": 0},
        ],
        queue_rows=[{"truck_id": "AQ009", "truck_type": "small_floor", "remaining": 16}],
    )
    with api_server_context(runtime) as base_url:
        run_minutes_via_api(base_url, 1)
        before_workers, before_forks = _assignments(runtime)
        kept_payload = keep_plan_via_api(base_url)
        assert_dashboard_matches_backend(runtime, kept_payload)
        after_workers, after_forks = _assignments(runtime)
        assert before_workers == after_workers
        assert before_forks == after_forks
        assert kept_payload["recommendation"]["is_applied"] is False
        assert kept_payload["recommendation"]["decision_status"] == "kept_current_plan"


def test_scenario_queue_buildup_api() -> None:
    runtime = make_seeded_runtime(
        seed=206,
        config_overrides={"arrival_rate_per_hour": 36.0, "clear_worker_rate": 0.2, "clear_forklift_rate": 0.2},
    )
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "large_floor", "truck_remaining": 50, "staging": 35, "workers": 2, "forklifts": 1},
            {"dock_id": 2, "truck_type": "medium_floor", "truck_remaining": 42, "staging": 32, "workers": 1, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 26, "staging": 22, "workers": 1, "forklifts": 1},
            {"dock_id": 4, "truck_type": "medium_floor", "truck_remaining": 36, "staging": 28, "workers": 1, "forklifts": 0},
        ],
        queue_rows=[{"truck_id": "AQ010", "truck_type": "small_floor", "remaining": 20}],
    )
    with api_server_context(runtime) as base_url:
        initial = get_state_via_api(base_url)["kpis"]["queue_length"]
        payload = run_minutes_via_api(base_url, 35)
        assert_dashboard_matches_backend(runtime, payload)
        assert payload["kpis"]["queue_length"] >= initial
        assert max(payload["trends"]["queue_length"]) >= initial
        assert payload["kpis"]["dock_utilization"] >= 50.0


def test_scenario_independent_dock_behavior_api() -> None:
    runtime = make_seeded_runtime(seed=207)
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "large_floor", "truck_remaining": 30, "staging": 88, "workers": 2, "forklifts": 0},
            {"dock_id": 2, "staging": 40, "workers": 1, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 18, "staging": 12, "workers": 0, "forklifts": 1},
            {"dock_id": 4, "staging": 0, "workers": 2, "forklifts": 1},
        ],
        queue_rows=[
            {"truck_id": "AQ011", "truck_type": "small_floor", "remaining": 14},
            {"truck_id": "AQ012", "truck_type": "medium_palletized", "remaining": 20},
            {"truck_id": "AQ013", "truck_type": "medium_floor", "remaining": 28},
        ],
    )
    with api_server_context(runtime) as base_url:
        dock4_received = False
        dock2_values = []
        dock1_values = []
        for _ in range(8):
            payload = run_minutes_via_api(base_url, 1)
            assert_dashboard_matches_backend(runtime, payload)
            dock2_values.append(runtime.state.docks[2].staging.occupancy_units)
            dock1_values.append(runtime.state.docks[1].staging.occupancy_units)
            if runtime.state.docks[4].current_truck is not None:
                dock4_received = True
        assert dock4_received
        assert dock2_values[-1] < dock2_values[0]
        assert dock1_values[-1] != dock2_values[-1]


def test_scenario_review_timer_trigger_api() -> None:
    runtime = make_seeded_runtime(
        seed=208,
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
    with api_server_context(runtime) as base_url:
        for _ in range(3):
            payload = run_minutes_via_api(base_url, 1)
            assert_dashboard_matches_backend(runtime, payload)
            assert payload["recommendation"]["minute_generated"] is None
        payload = run_minutes_via_api(base_url, 1)
        assert_dashboard_matches_backend(runtime, payload)
        assert "review_timer" in payload["recommendation"]["trigger_source"]


def test_scenario_supervisor_constraint_change_api() -> None:
    runtime = make_seeded_runtime(seed=209, config_overrides={"arrival_rate_per_hour": 8.0})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 28, "staging": 12, "workers": 2, "forklifts": 1},
            {"dock_id": 2, "truck_type": "large_floor", "truck_remaining": 30, "staging": 18, "workers": 1, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 16, "staging": 10, "workers": 1, "forklifts": 1},
            {"dock_id": 4, "staging": 0, "workers": 1, "forklifts": 0},
        ],
        queue_rows=[{"truck_id": "AQ014", "truck_type": "small_floor", "remaining": 18}],
    )
    with api_server_context(runtime) as base_url:
        run_minutes_via_api(base_url, 3)
        payload = update_supervisor_via_api(
            base_url,
            {"available_workers": 4, "available_forklifts": 2, "active_docks": 4, "max_unloaders_per_dock": 4},
        )
        assert_dashboard_matches_backend(runtime, payload)
        assert payload["resource_summary"]["workers_total"] == 4
        assert payload["resource_summary"]["forklifts_total"] == 2


def test_scenario_run15_matches_step15_api() -> None:
    runtime_a = make_seeded_runtime(seed=210, config_overrides={"arrival_rate_per_hour": 18.0, "review_interval_minutes": 5})
    seed_yard_state(
        runtime_a,
        dock_rows=[
            {"dock_id": 1, "truck_type": "medium_floor", "truck_remaining": 32, "staging": 16, "workers": 2, "forklifts": 1},
            {"dock_id": 2, "truck_type": "small_palletized", "truck_remaining": 20, "staging": 10, "workers": 0, "forklifts": 1},
            {"dock_id": 3, "truck_type": "medium_floor", "truck_remaining": 25, "staging": 14, "workers": 2, "forklifts": 0},
            {"dock_id": 4, "staging": 0, "workers": 1, "forklifts": 1},
        ],
        queue_rows=[
            {"truck_id": "AQ015", "truck_type": "small_floor", "remaining": 18},
            {"truck_id": "AQ016", "truck_type": "medium_floor", "remaining": 24},
        ],
    )
    runtime_b = copy.deepcopy(runtime_a)
    with api_server_context(runtime_a) as base_a, api_server_context(runtime_b) as base_b:
        payload_run = run_minutes_via_api(base_a, 15)
        for _ in range(15):
            payload_step = run_minutes_via_api(base_b, 1)
        assert_dashboard_matches_backend(runtime_a, payload_run)
        assert_dashboard_matches_backend(runtime_b, payload_step)
        assert payload_run["kpis"] == payload_step["kpis"]
        assert payload_run["dock_status"] == payload_step["dock_status"]
        assert payload_run["staging_status"] == payload_step["staging_status"]
        assert payload_run["resource_summary"] == payload_step["resource_summary"]
        assert payload_run["recommendation"] == payload_step["recommendation"]
        assert payload_run["trends"] == payload_step["trends"]


def test_scenario_simulated_arrivals_visible_api() -> None:
    runtime = make_seeded_runtime(seed=211, config_overrides={"arrival_rate_per_hour": 26.0, "review_interval_minutes": 5})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "large_floor", "truck_remaining": 45, "staging": 20, "workers": 2, "forklifts": 1},
            {"dock_id": 2, "truck_type": "medium_floor", "truck_remaining": 38, "staging": 18, "workers": 1, "forklifts": 1},
            {"dock_id": 3, "truck_type": "small_palletized", "truck_remaining": 22, "staging": 12, "workers": 1, "forklifts": 1},
            {"dock_id": 4, "truck_type": "medium_floor", "truck_remaining": 30, "staging": 16, "workers": 1, "forklifts": 0},
        ],
        queue_rows=[{"truck_id": "AQ017", "truck_type": "small_floor", "remaining": 14}],
    )
    with api_server_context(runtime) as base_url:
        payload = run_minutes_via_api(base_url, 25)
        assert_dashboard_matches_backend(runtime, payload)
        assert sum(payload["trends"]["arrivals"]) > 0
        assert max(payload["trends"]["queue_length"]) >= payload["trends"]["queue_length"][0]


def test_scenario_full_end_to_end_dashboard_truth_api() -> None:
    runtime = make_seeded_runtime(seed=212, config_overrides={"arrival_rate_per_hour": 14.0, "review_interval_minutes": 5, "min_score_improvement_to_switch": 0.0})
    seed_yard_state(
        runtime,
        dock_rows=[
            {"dock_id": 1, "truck_type": "large_floor", "truck_remaining": 36, "staging": 84.9, "workers": 4, "forklifts": 2},
            {"dock_id": 2, "truck_type": "medium_floor", "truck_remaining": 2, "staging": 20, "workers": 1, "forklifts": 1},
            {"dock_id": 3, "staging": 0, "workers": 0, "forklifts": 0},
            {"dock_id": 4, "truck_type": "small_palletized", "truck_remaining": 16, "staging": 8, "workers": 0, "forklifts": 0},
        ],
        queue_rows=[
            {"truck_id": "AQ018", "truck_type": "small_floor", "remaining": 12},
            {"truck_id": "AQ019", "truck_type": "medium_floor", "remaining": 20},
            {"truck_id": "AQ020", "truck_type": "small_palletized", "remaining": 15},
        ],
    )
    with api_server_context(runtime) as base_url:
        dock3_got_truck = False
        dock2_blocked_after_unload = False
        saw_threshold = False
        saw_recommendation = False
        payload = get_state_via_api(base_url)
        assert_dashboard_matches_backend(runtime, payload)
        for _ in range(20):
            payload = run_minutes_via_api(base_url, 1)
            assert_dashboard_matches_backend(runtime, payload)
            if runtime.state.docks[3].current_truck is not None:
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
        if runtime.state.last_recommendation is not None and runtime.state.last_recommendation.selected_action.action_name == "keep_current_plan":
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
                    truck_id="AQX-FULL",
                    truck_type="large_floor",
                    initial_load_units=50.0,
                    remaining_load_units=35.0,
                    gate_arrival_minute=runtime.state.now_minute,
                    assigned_dock_id=2,
                    unload_start_minute=runtime.state.now_minute,
                )
            runtime.state.update_resource_assignment_counters()
            payload = run_minutes_via_api(base_url, 1)
            assert_dashboard_matches_backend(runtime, payload)
        assert runtime.state.last_recommendation is not None
        assert runtime.state.last_recommendation.selected_action.action_name != "keep_current_plan"
        before_workers, before_forks = _assignments(runtime)
        apply_payload = apply_recommendation_via_api(base_url)
        assert_dashboard_matches_backend(runtime, apply_payload)
        after_workers, after_forks = _assignments(runtime)
        assert before_workers != after_workers or before_forks != after_forks
        final_payload = run_minutes_via_api(base_url, 6)
        assert_dashboard_matches_backend(runtime, final_payload)
        assert sum(final_payload["trends"]["arrivals"]) > 0

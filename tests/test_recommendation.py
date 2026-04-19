import random

import pytest

from yard.config import YardConfig
from yard.engine import apply_action, initialize_state
from yard.models import Action, ActionEvaluation, TriggerEvent, Truck, YardState
from yard.recommendation import build_candidate_actions, recommend_best_action
import yard.recommendation as recommendation_module
from yard.simulation import generate_arrivals_for_minute, simulate_one_minute


def _seed_busy_state() -> tuple[YardState, YardConfig]:
    config = YardConfig(arrival_rate_per_hour=0.0, lookahead_horizon_minutes=10)
    state = initialize_state(
        available_workers=6,
        available_forklifts=3,
        active_docks=3,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    baseline = Action(
        action_name="baseline",
        workers_by_dock={1: 3, 2: 2, 3: 1},
        forklifts_by_dock={1: 1, 2: 1, 3: 1},
    )
    apply_action(state, baseline, config=config)

    rng = random.Random(3)
    state.now_minute = 0
    for minute in range(5):
        state.now_minute = minute
        generate_arrivals_for_minute(state, YardConfig(arrival_rate_per_hour=120.0), rng)
    simulate_one_minute(state, config=config, rng=random.Random(7))
    return state, config


def test_candidate_feasibility() -> None:
    state, config = _seed_busy_state()

    actions = build_candidate_actions(state, config)
    assert actions
    action_names = {action.action_name for action in actions}
    assert "keep_current_plan" in action_names
    assert "prioritize_clearing_at_riskiest_dock" in action_names

    for action in actions:
        assert set(action.workers_by_dock.keys()) == set(state.docks.keys())
        assert set(action.forklifts_by_dock.keys()) == set(state.docks.keys())
        assert all(value >= 0 for value in action.workers_by_dock.values())
        assert all(value >= 0 for value in action.forklifts_by_dock.values())
        assert all(value <= config.max_unloaders_per_dock for value in action.workers_by_dock.values())
        assert sum(action.workers_by_dock.values()) <= state.resources.total_workers
        assert sum(action.forklifts_by_dock.values()) <= state.resources.total_forklifts


def _make_eval(
    action: Action,
    score: float,
    *,
    wait: float = 0.0,
    tis: float = 0.0,
    queue: float = 0.0,
    util: float = 0.0,
    risk: float = 0.0,
    throughput: float = 0.0,
    flow: float = 0.0,
) -> ActionEvaluation:
    return ActionEvaluation(
        action=action,
        predicted_avg_wait_minutes=wait,
        predicted_avg_time_in_system_minutes=tis,
        predicted_queue_length=queue,
        predicted_dock_utilization=util,
        predicted_staging_overflow_risk=risk,
        score=score,
        throughput_trucks_per_hour=throughput,
        effective_flow_rate_per_hour=flow,
        robust_score=score,
    )


def test_recommendation_selects_lowest_score(monkeypatch: pytest.MonkeyPatch) -> None:
    state = initialize_state(
        available_workers=2,
        available_forklifts=1,
        active_docks=1,
        max_unloaders_per_dock=4,
    )
    config = YardConfig(min_score_improvement_to_switch=0.01)

    def fake_evaluate_candidates(*args, **kwargs):
        return [
            _make_eval(Action("keep_current_plan", {1: 1}, {1: 0}), 42.0),
            _make_eval(Action("shift_one_worker_to_most_loaded_dock", {1: 2}, {1: 0}), 31.0),
            _make_eval(Action("shift_one_forklift_to_most_loaded_dock", {1: 1}, {1: 1}), 27.0),
        ]

    monkeypatch.setattr(recommendation_module, "evaluate_candidates", fake_evaluate_candidates)
    recommendation = recommend_best_action(state, config)
    assert recommendation is not None
    assert recommendation.selected_action.action_name == "shift_one_forklift_to_most_loaded_dock"


def test_switch_guard_blocks_small_improvements(monkeypatch: pytest.MonkeyPatch) -> None:
    state = initialize_state(
        available_workers=2,
        available_forklifts=1,
        active_docks=1,
        max_unloaders_per_dock=4,
    )
    config = YardConfig(min_score_improvement_to_switch=0.05)

    def fake_evaluate_candidates(*args, **kwargs):
        return [
            _make_eval(Action("keep_current_plan", {1: 1}, {1: 0}), 100.0),
            _make_eval(Action("shift_one_worker_to_most_loaded_dock", {1: 2}, {1: 0}), 96.5),
        ]

    monkeypatch.setattr(recommendation_module, "evaluate_candidates", fake_evaluate_candidates)
    recommendation = recommend_best_action(state, config)
    assert recommendation is not None
    assert recommendation.selected_action.action_name == "keep_current_plan"


def _seed_floor_dock(
    state: YardState,
    *,
    dock_id: int,
    remaining: float,
    staging: float,
    workers: int,
) -> None:
    dock = state.docks[dock_id]
    dock.current_truck = Truck(
        truck_id=f"F-{dock_id}",
        truck_type="medium_floor",
        initial_load_units=max(remaining, 1.0),
        remaining_load_units=remaining,
        gate_arrival_minute=0,
        assigned_dock_id=dock_id,
        unload_start_minute=0,
    )
    dock.staging.occupancy_units = staging
    dock.assigned_workers = workers
    dock.assigned_forklifts = 0


def _seed_pallet_dock(
    state: YardState,
    *,
    dock_id: int,
    remaining: float,
    staging: float,
    forklifts: int,
) -> None:
    dock = state.docks[dock_id]
    dock.current_truck = Truck(
        truck_id=f"P-{dock_id}",
        truck_type="medium_palletized",
        initial_load_units=max(remaining, 1.0),
        remaining_load_units=remaining,
        gate_arrival_minute=0,
        assigned_dock_id=dock_id,
        unload_start_minute=0,
    )
    dock.staging.occupancy_units = staging
    dock.assigned_workers = 0
    dock.assigned_forklifts = forklifts


def _action_by_name(actions: list[Action], action_name: str) -> Action:
    return next(action for action in actions if action.action_name == action_name)


def test_worker_shift_prefers_idle_pool_before_reallocation() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0)
    state = initialize_state(
        available_workers=8,
        available_forklifts=0,
        active_docks=4,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    _seed_floor_dock(state, dock_id=1, remaining=20.0, staging=10.0, workers=1)
    _seed_floor_dock(state, dock_id=2, remaining=60.0, staging=25.0, workers=1)
    _seed_floor_dock(state, dock_id=3, remaining=8.0, staging=2.0, workers=2)
    _seed_floor_dock(state, dock_id=4, remaining=6.0, staging=1.0, workers=0)
    state.update_resource_assignment_counters()
    assert state.resources.idle_workers == 4

    action = _action_by_name(build_candidate_actions(state, config), "shift_one_worker_to_most_loaded_dock")
    assert action.workers_by_dock[1] == 1
    assert action.workers_by_dock[2] == 2
    assert action.workers_by_dock[3] == 2
    assert action.workers_by_dock[4] == 0
    assert "source=idle_pool" in action.notes


def test_forklift_shift_prefers_idle_pool_before_reallocation() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0)
    state = initialize_state(
        available_workers=2,
        available_forklifts=4,
        active_docks=3,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    _seed_pallet_dock(state, dock_id=1, remaining=20.0, staging=10.0, forklifts=1)
    _seed_pallet_dock(state, dock_id=2, remaining=54.0, staging=20.0, forklifts=1)
    _seed_floor_dock(state, dock_id=3, remaining=18.0, staging=4.0, workers=1)
    state.update_resource_assignment_counters()
    assert state.resources.idle_forklifts == 2

    action = _action_by_name(build_candidate_actions(state, config), "shift_one_forklift_to_most_loaded_dock")
    assert action.forklifts_by_dock[1] == 1
    assert action.forklifts_by_dock[2] == 2
    assert "source=idle_pool" in action.notes


def test_worker_shift_reallocates_only_when_no_idle_workers() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0)
    state = initialize_state(
        available_workers=4,
        available_forklifts=0,
        active_docks=3,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    _seed_floor_dock(state, dock_id=1, remaining=12.0, staging=7.0, workers=2)
    _seed_floor_dock(state, dock_id=2, remaining=44.0, staging=25.0, workers=1)
    _seed_floor_dock(state, dock_id=3, remaining=10.0, staging=3.0, workers=1)
    state.update_resource_assignment_counters()
    assert state.resources.idle_workers == 0

    action = _action_by_name(build_candidate_actions(state, config), "shift_one_worker_to_most_loaded_dock")
    assert action.workers_by_dock[1] == 1
    assert action.workers_by_dock[2] == 2
    assert action.workers_by_dock[3] == 1
    assert "source=shifted_from_dock_1" in action.notes


def test_forklift_shift_reallocates_only_when_no_idle_forklifts() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0)
    state = initialize_state(
        available_workers=1,
        available_forklifts=2,
        active_docks=2,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    _seed_pallet_dock(state, dock_id=1, remaining=18.0, staging=8.0, forklifts=1)
    _seed_pallet_dock(state, dock_id=2, remaining=40.0, staging=20.0, forklifts=1)
    state.update_resource_assignment_counters()
    assert state.resources.idle_forklifts == 0

    action = _action_by_name(build_candidate_actions(state, config), "shift_one_forklift_to_most_loaded_dock")
    assert action.forklifts_by_dock[1] == 0
    assert action.forklifts_by_dock[2] == 2
    assert "source=shifted_from_dock_1" in action.notes


def test_clearing_candidate_prefers_idle_pool_and_preserves_exclusive_family() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0)
    state = initialize_state(
        available_workers=5,
        available_forklifts=1,
        active_docks=3,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    _seed_floor_dock(state, dock_id=1, remaining=16.0, staging=40.0, workers=2)
    _seed_floor_dock(state, dock_id=2, remaining=18.0, staging=92.0, workers=1)
    _seed_pallet_dock(state, dock_id=3, remaining=24.0, staging=20.0, forklifts=1)
    state.update_resource_assignment_counters()
    assert state.resources.idle_workers == 2

    action = _action_by_name(build_candidate_actions(state, config), "prioritize_clearing_at_riskiest_dock")
    assert action.workers_by_dock[1] == 2
    assert action.workers_by_dock[2] == 2
    assert action.workers_by_dock[3] == 0
    assert action.forklifts_by_dock[3] == 1
    assert "source=idle_pool" in action.notes


def test_recommendation_rationale_includes_target_source_and_kpi_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = initialize_state(
        available_workers=4,
        available_forklifts=0,
        active_docks=2,
        max_unloaders_per_dock=4,
    )
    _seed_floor_dock(state, dock_id=1, remaining=14.0, staging=8.0, workers=1)
    _seed_floor_dock(state, dock_id=2, remaining=48.0, staging=22.0, workers=1)
    state.update_resource_assignment_counters()
    config = YardConfig(min_score_improvement_to_switch=0.0)

    keep_action = Action(
        action_name="keep_current_plan",
        workers_by_dock={1: 1, 2: 1},
        forklifts_by_dock={1: 0, 2: 0},
    )
    shift_action = Action(
        action_name="shift_one_worker_to_most_loaded_dock",
        workers_by_dock={1: 1, 2: 2},
        forklifts_by_dock={1: 0, 2: 0},
        notes="Allocate one idle worker to Dock 2 (source=idle_pool).",
    )

    def fake_evaluate_candidates(*args, **kwargs):
        return [
            _make_eval(
                keep_action,
                100.0,
                wait=18.0,
                tis=82.0,
                queue=9.2,
                util=1.0,
                risk=0.20,
                throughput=6.0,
                flow=7.0,
            ),
            _make_eval(
                shift_action,
                80.0,
                wait=15.0,
                tis=78.0,
                queue=7.8,
                util=1.0,
                risk=0.00,
                throughput=7.0,
                flow=8.0,
            ),
        ]

    monkeypatch.setattr(recommendation_module, "evaluate_candidates", fake_evaluate_candidates)
    recommendation = recommend_best_action(
        state,
        config,
        trigger_event=TriggerEvent(
            trigger_type="review_timer",
            minute=1,
            dock_id=None,
            reason="Review interval reached.",
        ),
    )
    assert recommendation is not None
    assert recommendation.selected_action.action_name == "shift_one_worker_to_most_loaded_dock"
    assert recommendation.selected_target_dock_id == 2
    assert "Dock 2" in recommendation.selected_dock_reason
    assert "idle pool" in recommendation.resource_source_reason.lower()
    assert "Review interval reached." in recommendation.rationale
    assert "improves from 18.0 to 15.0 minutes" in recommendation.rationale
    assert recommendation.kpi_delta["predicted_avg_wait_minutes"] == {
        "before": 18.0,
        "after": 15.0,
        "delta": -3.0,
    }

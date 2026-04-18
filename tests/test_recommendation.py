import random

import pytest

from yard.config import YardConfig
from yard.engine import apply_action, initialize_state
from yard.models import Action, ActionEvaluation, YardState
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


def _make_eval(action_name: str, score: float) -> ActionEvaluation:
    return ActionEvaluation(
        action=Action(
            action_name=action_name,
            workers_by_dock={1: 1},
            forklifts_by_dock={1: 0},
            hold_gate_release=False,
        ),
        predicted_avg_wait_minutes=0.0,
        predicted_avg_time_in_system_minutes=0.0,
        predicted_queue_length=0.0,
        predicted_dock_utilization=0.0,
        predicted_staging_overflow_risk=0.0,
        score=score,
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
            _make_eval("keep_current_plan", 42.0),
            _make_eval("shift_one_worker_to_most_loaded_dock", 31.0),
            _make_eval("shift_one_forklift_to_most_loaded_dock", 27.0),
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
            _make_eval("keep_current_plan", 100.0),
            _make_eval("shift_one_worker_to_most_loaded_dock", 96.5),
        ]

    monkeypatch.setattr(recommendation_module, "evaluate_candidates", fake_evaluate_candidates)
    recommendation = recommend_best_action(state, config)
    assert recommendation is not None
    assert recommendation.selected_action.action_name == "keep_current_plan"

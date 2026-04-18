import random

from yard.config import YardConfig
from yard.engine import build_ise_output, initialize_state, run_minute_cycle
from yard.evaluation import evaluate_action_across_scenarios
from yard.forecasting import build_forecast
from yard.models import Action, Truck
from yard.verification import ci_half_width_ratio, littles_law_check


def test_forecast_generates_low_baseline_high_scenarios() -> None:
    config = YardConfig(
        arrival_rate_per_hour=12.0,
        forecast_window_minutes=10,
        forecast_smoothing_alpha=0.5,
        forecast_scenario_delta=0.2,
    )
    state = initialize_state(
        available_workers=3,
        available_forklifts=1,
        active_docks=2,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    state.now_minute = 12
    state.arrival_history = [(3, 1), (4, 0), (5, 2), (8, 1), (10, 1), (11, 0), (12, 2)]
    state.kpi_cache["smoothed_arrival_rate_per_hour"] = 9.0

    forecast = build_forecast(state, config)

    assert forecast.baseline_rate_per_hour >= 0.0
    assert forecast.smoothed_rate_per_hour >= 0.0
    assert forecast.scenarios["low"] <= forecast.scenarios["baseline"] <= forecast.scenarios["high"]
    assert forecast.expected_arrivals >= 0.0


def test_evaluation_produces_replication_scenario_metrics_and_verification() -> None:
    config = YardConfig(
        arrival_rate_per_hour=10.0,
        lookahead_horizon_minutes=10,
        evaluation_replications=3,
    )
    state = initialize_state(
        available_workers=4,
        available_forklifts=2,
        active_docks=2,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    state.waiting_queue.extend(
        [
            Truck("T-EVAL-1", "small_floor", 30.0, 30.0, 0),
            Truck("T-EVAL-2", "small_palletized", 24.0, 24.0, 0),
        ]
    )
    action = Action(
        action_name="test_eval",
        workers_by_dock={1: 2, 2: 2},
        forklifts_by_dock={1: 1, 2: 1},
        hold_gate_release=False,
    )
    evaluation = evaluate_action_across_scenarios(
        state=state,
        action=action,
        config=config,
        scenario_rates={"low": 8.0, "baseline": 10.0, "high": 12.0},
        rng_seed=91,
    )

    assert set(evaluation.scenario_metrics.keys()) == {"low", "baseline", "high"}
    assert evaluation.replication_count == 3
    assert evaluation.robust_score >= evaluation.score
    assert evaluation.predicted_avg_time_in_system_minutes >= 0.0
    assert evaluation.throughput_trucks_per_hour >= 0.0
    assert "spec_3_littles_law" in evaluation.verification
    assert "spec_4_ci_halfwidth" in evaluation.verification


def test_recommendation_and_ise_output_are_enriched() -> None:
    config = YardConfig(
        arrival_rate_per_hour=8.0,
        review_interval_minutes=1,
        lookahead_horizon_minutes=8,
        evaluation_replications=2,
    )
    state = initialize_state(
        available_workers=4,
        available_forklifts=2,
        active_docks=2,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    state.waiting_queue.append(Truck("T-ISE-1", "medium_floor", 50.0, 50.0, 0))

    _, recommendation = run_minute_cycle(state, config=config, rng=random.Random(8))
    assert recommendation is not None
    assert recommendation.forecast is not None
    assert recommendation.evaluations
    assert recommendation.selected_baseline_metrics
    assert recommendation.verification

    ise_output = build_ise_output(state, config=config)
    assert set(ise_output.keys()) == {
        "snapshot",
        "forecast",
        "best_recommendation",
        "verification",
        "evaluations",
    }
    assert ise_output["best_recommendation"] is not None
    assert isinstance(ise_output["evaluations"], list)
    first_eval = ise_output["evaluations"][0]
    assert "scenarios" in first_eval
    assert set(first_eval["scenarios"].keys()) == {"low", "baseline", "high"}


def test_verification_utilities_return_expected_shape() -> None:
    spec3 = littles_law_check(
        arrival_rate_per_hour=30.0,
        avg_time_in_system_minutes=20.0,
        avg_number_in_system=9.5,
        threshold=0.25,
    )
    spec4 = ci_half_width_ratio(
        replication_means=[18.5, 19.0, 21.0, 20.0, 19.8],
        threshold=0.30,
    )

    assert "relative_error" in spec3
    assert "pass" in spec3
    assert "half_width" in spec4
    assert "ratio" in spec4
    assert "pass" in spec4


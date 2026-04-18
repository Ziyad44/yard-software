"""Top-level engine orchestration for version-1 yard cycle."""

from __future__ import annotations

from dataclasses import asdict
import random

from .config import YardConfig
from .forecasting import build_forecast
from .models import (
    Action,
    ActionEvaluation,
    DockState,
    DockSummary,
    Recommendation,
    ScenarioMetrics,
    ResourcePool,
    StagingAreaState,
    SystemSnapshot,
    TriggerEvent,
    YardState,
)
from .recommendation import recommend_best_action
from .simulation import sanitize_assignment_for_dock, simulate_horizon, simulate_one_minute


def initialize_state(
    *,
    available_workers: int,
    available_forklifts: int,
    active_docks: int,
    max_unloaders_per_dock: int,
    config: YardConfig | None = None,
) -> YardState:
    """Create a clean live yard state from supervisor startup inputs."""
    cfg = config or YardConfig(max_unloaders_per_dock=max_unloaders_per_dock)
    state = YardState(
        now_minute=0,
        resources=ResourcePool(
            total_workers=max(int(available_workers), 0),
            total_forklifts=max(int(available_forklifts), 0),
        ),
        next_review_minute=max(cfg.review_interval_minutes, 1),
    )

    for dock_id in range(1, max(int(active_docks), 0) + 1):
        state.docks[dock_id] = DockState(
            dock_id=dock_id,
            active=True,
            assigned_workers=0,
            assigned_forklifts=0,
            staging=StagingAreaState(
                dock_id=dock_id,
                occupancy_units=0.0,
                capacity_units=cfg.staging_capacity_units,
                threshold_high=cfg.staging_high_threshold,
                threshold_low=cfg.staging_low_threshold,
            ),
        )

    return state


def update_supervisor_inputs(
    state: YardState,
    *,
    available_workers: int | None = None,
    available_forklifts: int | None = None,
    active_docks: int | None = None,
) -> None:
    """Update live supervisor controls without resetting queue or docks."""
    if available_workers is not None:
        state.resources.total_workers = max(int(available_workers), 0)
    if available_forklifts is not None:
        state.resources.total_forklifts = max(int(available_forklifts), 0)

    if active_docks is not None:
        new_active = max(int(active_docks), 0)
        current_max = max(state.docks.keys(), default=0)
        for dock_id in range(1, max(current_max, new_active) + 1):
            if dock_id not in state.docks:
                state.docks[dock_id] = DockState(
                    dock_id=dock_id,
                    active=False,
                    staging=StagingAreaState(dock_id=dock_id),
                )
            # New docks start active only when they are within the requested count.
            if dock_id > current_max:
                state.docks[dock_id].active = dock_id <= new_active

        active_ids = sorted(dock_id for dock_id, dock in state.docks.items() if dock.active)
        current_active = len(active_ids)

        if new_active > current_active:
            inactive_ids = sorted(dock_id for dock_id, dock in state.docks.items() if not dock.active)
            for dock_id in inactive_ids:
                if current_active >= new_active:
                    break
                state.docks[dock_id].active = True
                current_active += 1
        elif new_active < current_active:
            # Deactivate only truly free docks so in-progress operations do not freeze.
            for dock_id in sorted(active_ids, reverse=True):
                if current_active <= new_active:
                    break
                dock = state.docks[dock_id]
                if not dock.can_accept_next_truck():
                    continue
                dock.active = False
                dock.assigned_workers = 0
                dock.assigned_forklifts = 0
                current_active -= 1

    state.update_resource_assignment_counters()


def apply_action(state: YardState, action: Action, config: YardConfig) -> None:
    """Apply a recommendation payload to live dock assignments."""
    worker_total = 0
    forklift_total = 0
    normalized_workers_by_dock: dict[int, int] = {}
    normalized_forklifts_by_dock: dict[int, int] = {}

    for dock_id, dock in state.docks.items():
        if not dock.active:
            continue
        workers, forklifts = sanitize_assignment_for_dock(
            dock=dock,
            workers=int(action.workers_by_dock.get(dock_id, 0)),
            forklifts=int(action.forklifts_by_dock.get(dock_id, 0)),
            max_unloaders_per_dock=config.max_unloaders_per_dock,
        )
        normalized_workers_by_dock[dock_id] = workers
        normalized_forklifts_by_dock[dock_id] = forklifts
        worker_total += workers
        forklift_total += forklifts

    if worker_total > state.resources.total_workers:
        raise ValueError("Action assigns more workers than available.")
    if forklift_total > state.resources.total_forklifts:
        raise ValueError("Action assigns more forklifts than available.")

    for dock_id, dock in state.docks.items():
        if not dock.active:
            dock.assigned_workers = 0
            dock.assigned_forklifts = 0
            continue
        dock.assigned_workers = normalized_workers_by_dock.get(dock_id, 0)
        dock.assigned_forklifts = normalized_forklifts_by_dock.get(dock_id, 0)

    state.hold_gate_release = bool(action.hold_gate_release)
    state.active_action = Action(
        action_name=action.action_name,
        workers_by_dock=normalized_workers_by_dock,
        forklifts_by_dock=normalized_forklifts_by_dock,
        hold_gate_release=bool(action.hold_gate_release),
        notes=action.notes,
    )
    state.update_resource_assignment_counters()


def recommend_on_triggers(
    state: YardState,
    triggers: list[TriggerEvent],
    config: YardConfig,
) -> Recommendation | None:
    """Run recommendation only when at least one trigger exists."""
    if not triggers:
        return None
    recommendation = recommend_best_action(state, config=config)
    state.last_recommendation = recommendation
    if recommendation is None:
        state.recent_replication_means = []
    else:
        selected_name = recommendation.selected_action.action_name
        selected_eval = next(
            (
                evaluation
                for evaluation in recommendation.evaluations
                if evaluation.action.action_name == selected_name
            ),
            None,
        )
        state.recent_replication_means = (
            list(selected_eval.replication_avg_tis) if selected_eval is not None else []
        )
    return recommendation


def run_minute_cycle(
    state: YardState,
    config: YardConfig,
    rng: random.Random | None = None,
) -> tuple[list[TriggerEvent], Recommendation | None]:
    """
    Execute one live minute:
    - simulate state forward,
    - detect triggers,
    - produce recommendation if triggered.
    """
    cycle_rng = rng if rng is not None else random
    triggers = simulate_one_minute(state, config=config, rng=cycle_rng)
    recommendation = recommend_on_triggers(state, triggers, config=config)
    refresh_kpi_cache(state, config=config)
    return triggers, recommendation


def refresh_kpi_cache(state: YardState, config: YardConfig) -> None:
    """Refresh short-horizon KPI estimates from the current live plan."""
    forecast = build_forecast(state, config)
    snapshot = simulate_horizon(
        state,
        config=config,
        minutes=config.lookahead_horizon_minutes,
        rng=random.Random(state.now_minute + 1009),
    )
    state.kpi_cache.update(
        {
            "predicted_avg_wait_minutes": float(snapshot.predicted_avg_wait_minutes or 0.0),
            "predicted_avg_time_in_system_minutes": float(
                snapshot.predicted_avg_time_in_system_minutes or 0.0
            ),
            "predicted_queue_length": float(snapshot.predicted_queue_length or 0.0),
            "predicted_avg_number_in_system": float(snapshot.predicted_avg_number_in_system or 0.0),
            "predicted_dock_utilization": float(snapshot.predicted_dock_utilization or 0.0),
            "predicted_staging_overflow_risk": float(snapshot.predicted_staging_overflow_risk or 0.0),
            "predicted_throughput_trucks_per_hour": float(snapshot.predicted_throughput_trucks_per_hour or 0.0),
            "predicted_effective_flow_rate_per_hour": float(
                snapshot.predicted_effective_flow_rate_per_hour or 0.0
            ),
            "baseline_arrival_rate_per_hour": float(forecast.baseline_rate_per_hour),
            "smoothed_arrival_rate_per_hour": float(forecast.smoothed_rate_per_hour),
            "smoothed_arrival_rate_minute": float(state.now_minute),
            "expected_arrivals_lookahead": float(forecast.expected_arrivals),
            "forecast_low_rate_per_hour": float(forecast.scenarios.get("low", 0.0)),
            "forecast_baseline_rate_per_hour": float(forecast.scenarios.get("baseline", 0.0)),
            "forecast_high_rate_per_hour": float(forecast.scenarios.get("high", 0.0)),
            "forecast_window_minutes": float(forecast.window_minutes),
            "forecast_observed_arrivals": float(forecast.observed_arrivals),
        }
    )
    state.last_ise_output = build_ise_output(state, config=config)


def snapshot_from_state(state: YardState) -> SystemSnapshot:
    """Build a lightweight snapshot for dashboard/API use."""
    dock_summaries: list[DockSummary] = []
    for dock_id in sorted(state.docks):
        dock = state.docks[dock_id]
        if not dock.active:
            continue
        truck = dock.current_truck
        dock_summaries.append(
            DockSummary(
                dock_id=dock_id,
                phase=dock.phase,
                current_truck_id=truck.truck_id if truck else None,
                current_truck_type=truck.truck_type if truck else None,
                remaining_load_units=truck.remaining_load_units if truck else 0.0,
                staging_occupancy_units=dock.staging.occupancy_units,
                staging_capacity_units=dock.staging.capacity_units,
                assigned_workers=dock.assigned_workers,
                assigned_forklifts=dock.assigned_forklifts,
            )
        )

    recommendation_text = state.last_recommendation.rationale if state.last_recommendation else None
    staging_risk = 0.0
    dock_utilization = 0.0
    number_in_system = float(state.queue_length)
    if dock_summaries:
        dock_utilization = sum(1.0 for summary in dock_summaries if summary.phase != "idle") / len(dock_summaries)
        number_in_system += sum(1.0 for summary in dock_summaries if summary.current_truck_id)
        staging_risk = sum(
            1.0
            for summary in dock_summaries
            if summary.staging_capacity_units > 0.0
            and (summary.staging_occupancy_units / summary.staging_capacity_units) >= 0.85
        ) / len(dock_summaries)

    predicted_wait = state.kpi_cache.get("predicted_avg_wait_minutes")
    if predicted_wait is None:
        predicted_wait = float(state.queue_length)

    predicted_tis = state.kpi_cache.get("predicted_avg_time_in_system_minutes")
    if predicted_tis is None:
        completed_tis = [
            float(truck.total_time_in_system_minutes)
            for truck in state.completed_trucks
            if truck.total_time_in_system_minutes is not None
        ]
        if completed_tis:
            predicted_tis = sum(completed_tis) / len(completed_tis)
        else:
            predicted_tis = float(predicted_wait)

    predicted_util = state.kpi_cache.get("predicted_dock_utilization", dock_utilization)
    predicted_risk = state.kpi_cache.get("predicted_staging_overflow_risk", staging_risk)
    predicted_queue = state.kpi_cache.get("predicted_queue_length", float(state.queue_length))
    predicted_nsys = state.kpi_cache.get("predicted_avg_number_in_system", number_in_system)
    predicted_throughput = state.kpi_cache.get("predicted_throughput_trucks_per_hour", 0.0)
    predicted_effective_flow = state.kpi_cache.get("predicted_effective_flow_rate_per_hour", predicted_throughput)

    baseline_rate = float(state.kpi_cache.get("baseline_arrival_rate_per_hour", 0.0))
    smoothed_rate = float(state.kpi_cache.get("smoothed_arrival_rate_per_hour", baseline_rate))
    expected_arrivals = float(state.kpi_cache.get("expected_arrivals_lookahead", 0.0))
    forecast = {
        "baseline_rate_per_hour": baseline_rate,
        "smoothed_rate_per_hour": smoothed_rate,
        "expected_arrivals": expected_arrivals,
        "scenarios": {
            "low": float(state.kpi_cache.get("forecast_low_rate_per_hour", max(smoothed_rate * 0.8, 0.0))),
            "baseline": float(state.kpi_cache.get("forecast_baseline_rate_per_hour", max(smoothed_rate, 0.0))),
            "high": float(state.kpi_cache.get("forecast_high_rate_per_hour", max(smoothed_rate * 1.2, 0.0))),
        },
        "window_minutes": int(state.kpi_cache.get("forecast_window_minutes", 0.0)),
        "observed_arrivals": int(state.kpi_cache.get("forecast_observed_arrivals", 0.0)),
    }

    verification_details = (
        state.last_recommendation.verification
        if state.last_recommendation is not None
        else {}
    )
    ise_evaluations = (
        [serialize_action_evaluation(evaluation) for evaluation in state.last_recommendation.evaluations]
        if state.last_recommendation is not None
        else []
    )
    if isinstance(forecast, dict):
        forecast_summary = {
            "baseline_rate_per_hour": float(forecast["baseline_rate_per_hour"]),
            "smoothed_rate_per_hour": float(forecast["smoothed_rate_per_hour"]),
            "expected_arrivals": float(forecast["expected_arrivals"]),
            "scenarios": dict(forecast["scenarios"]),
            "window_minutes": int(forecast["window_minutes"]),
            "observed_arrivals": int(forecast["observed_arrivals"]),
        }
    else:
        forecast_summary = {
            "baseline_rate_per_hour": forecast.baseline_rate_per_hour,
            "smoothed_rate_per_hour": forecast.smoothed_rate_per_hour,
            "expected_arrivals": forecast.expected_arrivals,
            "scenarios": dict(forecast.scenarios),
            "window_minutes": forecast.window_minutes,
            "observed_arrivals": forecast.observed_arrivals,
        }

    return SystemSnapshot(
        minute=state.now_minute,
        queue_length=state.queue_length,
        predicted_avg_wait_minutes=predicted_wait,
        predicted_avg_time_in_system_minutes=predicted_tis,
        predicted_dock_utilization=predicted_util,
        predicted_staging_overflow_risk=predicted_risk,
        recommended_action_text=recommendation_text,
        predicted_queue_length=predicted_queue,
        predicted_avg_number_in_system=predicted_nsys,
        predicted_throughput_trucks_per_hour=predicted_throughput,
        predicted_effective_flow_rate_per_hour=predicted_effective_flow,
        forecast_summary=forecast_summary,
        verification_details=verification_details,
        ise_evaluations=ise_evaluations,
        docks=dock_summaries,
        resource_summary={
            "workers_total": state.resources.total_workers,
            "workers_assigned": state.resources.assigned_workers,
            "workers_idle": state.resources.idle_workers,
            "forklifts_total": state.resources.total_forklifts,
            "forklifts_assigned": state.resources.assigned_forklifts,
            "forklifts_idle": state.resources.idle_forklifts,
        },
        verification_placeholder={
            "spec_3_littles_law": "pending_phase_2",
            "spec_4_ci_halfwidth": "pending_phase_2",
        },
    )


def _serialize_scenario_metrics(metrics: ScenarioMetrics) -> dict[str, float | str]:
    return {
        "scenario_name": metrics.scenario_name,
        "arrival_rate_per_hour": metrics.arrival_rate_per_hour,
        "predicted_avg_wait_minutes": metrics.predicted_avg_wait_minutes,
        "predicted_avg_time_in_system_minutes": metrics.predicted_avg_time_in_system_minutes,
        "predicted_queue_length": metrics.predicted_queue_length,
        "predicted_avg_number_in_system": metrics.predicted_avg_number_in_system,
        "predicted_dock_utilization": metrics.predicted_dock_utilization,
        "predicted_staging_overflow_risk": metrics.predicted_staging_overflow_risk,
        "throughput_trucks_per_hour": metrics.throughput_trucks_per_hour,
        "effective_flow_rate_per_hour": metrics.effective_flow_rate_per_hour,
        "score": metrics.score,
    }


def serialize_action_evaluation(evaluation: ActionEvaluation) -> dict[str, object]:
    return {
        "action_name": evaluation.action.action_name,
        "action": {
            "workers_by_dock": dict(evaluation.action.workers_by_dock),
            "forklifts_by_dock": dict(evaluation.action.forklifts_by_dock),
            "hold_gate_release": bool(evaluation.action.hold_gate_release),
            "notes": evaluation.action.notes,
        },
        "baseline": {
            "predicted_avg_wait_minutes": evaluation.predicted_avg_wait_minutes,
            "predicted_avg_time_in_system_minutes": evaluation.predicted_avg_time_in_system_minutes,
            "predicted_queue_length": evaluation.predicted_queue_length,
            "predicted_avg_number_in_system": evaluation.predicted_avg_number_in_system,
            "predicted_dock_utilization": evaluation.predicted_dock_utilization,
            "predicted_staging_overflow_risk": evaluation.predicted_staging_overflow_risk,
            "throughput_trucks_per_hour": evaluation.throughput_trucks_per_hour,
            "effective_flow_rate_per_hour": evaluation.effective_flow_rate_per_hour,
            "score": evaluation.score,
        },
        "robust_score": evaluation.robust_score if evaluation.robust_score > 0 else evaluation.score,
        "replication_count": evaluation.replication_count,
        "replication_avg_tis": list(evaluation.replication_avg_tis),
        "verification": dict(evaluation.verification),
        "scenarios": {
            name: _serialize_scenario_metrics(metrics)
            for name, metrics in evaluation.scenario_metrics.items()
        },
    }


def build_ise_output(state: YardState, config: YardConfig) -> dict[str, object]:
    snapshot = snapshot_from_state(state)
    recommendation = state.last_recommendation
    forecast = build_forecast(state, config)

    evaluations = (
        [serialize_action_evaluation(evaluation) for evaluation in recommendation.evaluations]
        if recommendation is not None
        else []
    )
    best_recommendation = None
    verification = {}
    if recommendation is not None:
        best_recommendation = {
            "action_name": recommendation.selected_action.action_name,
            "action_payload": {
                "workers_by_dock": dict(recommendation.selected_action.workers_by_dock),
                "forklifts_by_dock": dict(recommendation.selected_action.forklifts_by_dock),
                "hold_gate_release": bool(recommendation.selected_action.hold_gate_release),
                "notes": recommendation.selected_action.notes,
            },
            "score": recommendation.score,
            "robust_score": recommendation.robust_score if recommendation.robust_score > 0 else recommendation.score,
            "rationale": recommendation.rationale,
            "expected_outcomes": dict(recommendation.selected_baseline_metrics),
        }
        verification = dict(recommendation.verification)

    return {
        "snapshot": asdict(snapshot),
        "forecast": {
            "baseline_rate_per_hour": forecast.baseline_rate_per_hour,
            "smoothed_rate_per_hour": forecast.smoothed_rate_per_hour,
            "expected_arrivals": forecast.expected_arrivals,
            "scenarios": dict(forecast.scenarios),
            "window_minutes": forecast.window_minutes,
            "observed_arrivals": forecast.observed_arrivals,
        },
        "best_recommendation": best_recommendation,
        "verification": verification,
        "evaluations": evaluations,
    }

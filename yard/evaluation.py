"""Scenario + replication evaluation utilities for candidate yard actions."""

from __future__ import annotations

import copy
import dataclasses
import random
import statistics

from .config import YardConfig
from .models import Action, ActionEvaluation, ScenarioMetrics, YardState
from .simulation import sanitize_assignment_for_dock, simulate_horizon
from .verification import build_verification_bundle


def _apply_action_assignments(state: YardState, action: Action, *, config: YardConfig) -> None:
    for dock_id, dock in state.docks.items():
        if not dock.active:
            dock.assigned_workers = 0
            dock.assigned_forklifts = 0
            continue
        workers, forklifts = sanitize_assignment_for_dock(
            dock=dock,
            workers=int(action.workers_by_dock.get(dock_id, 0)),
            forklifts=int(action.forklifts_by_dock.get(dock_id, 0)),
            max_unloaders_per_dock=config.max_unloaders_per_dock,
        )
        dock.assigned_workers = workers
        dock.assigned_forklifts = forklifts

    state.hold_gate_release = bool(action.hold_gate_release)
    state.update_resource_assignment_counters()


def _score(
    *,
    avg_wait: float,
    avg_tis: float,
    avg_queue: float,
    staging_risk: float,
    utilization: float,
) -> float:
    utilization_penalty = max(utilization - 0.92, 0.0) * 12.0
    return (
        0.8 * max(avg_wait, 0.0)
        + 1.0 * max(avg_tis, 0.0)
        + 0.4 * max(avg_queue, 0.0)
        + 25.0 * max(staging_risk, 0.0)
        + utilization_penalty
    )


def _aggregate_metric(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def evaluate_action_across_scenarios(
    *,
    state: YardState,
    action: Action,
    config: YardConfig,
    scenario_rates: dict[str, float],
    rng_seed: int,
) -> ActionEvaluation:
    applied_state = copy.deepcopy(state)
    _apply_action_assignments(applied_state, action, config=config)

    scenario_outputs: dict[str, ScenarioMetrics] = {}
    baseline_replication_tis: list[float] = []
    replications = max(int(config.evaluation_replications), 1)

    for scenario_index, (scenario_name, arrival_rate) in enumerate(scenario_rates.items()):
        waits: list[float] = []
        tis_values: list[float] = []
        avg_queues: list[float] = []
        avg_numbers: list[float] = []
        utilizations: list[float] = []
        staging_risks: list[float] = []
        throughputs: list[float] = []
        flow_rates: list[float] = []

        scenario_config = dataclasses.replace(config, arrival_rate_per_hour=max(float(arrival_rate), 0.0))
        for rep in range(replications):
            seed = rng_seed + scenario_index * 1000 + rep * 17
            snapshot = simulate_horizon(
                applied_state,
                config=scenario_config,
                minutes=config.lookahead_horizon_minutes,
                rng=random.Random(seed),
            )
            waits.append(float(snapshot.predicted_avg_wait_minutes or 0.0))
            tis_values.append(float(snapshot.predicted_avg_time_in_system_minutes or 0.0))
            avg_queues.append(float(snapshot.predicted_queue_length or 0.0))
            avg_numbers.append(float(snapshot.predicted_avg_number_in_system or 0.0))
            utilizations.append(float(snapshot.predicted_dock_utilization or 0.0))
            staging_risks.append(float(snapshot.predicted_staging_overflow_risk or 0.0))
            throughputs.append(float(snapshot.predicted_throughput_trucks_per_hour or 0.0))
            flow_rates.append(float(snapshot.predicted_effective_flow_rate_per_hour or 0.0))

        avg_wait = _aggregate_metric(waits)
        avg_tis = _aggregate_metric(tis_values)
        avg_queue = _aggregate_metric(avg_queues)
        avg_number = _aggregate_metric(avg_numbers)
        avg_util = _aggregate_metric(utilizations)
        avg_risk = _aggregate_metric(staging_risks)
        avg_throughput = _aggregate_metric(throughputs)
        avg_flow = _aggregate_metric(flow_rates)
        score = _score(
            avg_wait=avg_wait,
            avg_tis=avg_tis,
            avg_queue=avg_queue,
            staging_risk=avg_risk,
            utilization=avg_util,
        )

        scenario_outputs[scenario_name] = ScenarioMetrics(
            scenario_name=scenario_name,
            arrival_rate_per_hour=max(float(arrival_rate), 0.0),
            predicted_avg_wait_minutes=avg_wait,
            predicted_avg_time_in_system_minutes=avg_tis,
            predicted_queue_length=avg_queue,
            predicted_avg_number_in_system=avg_number,
            predicted_dock_utilization=avg_util,
            predicted_staging_overflow_risk=avg_risk,
            throughput_trucks_per_hour=avg_throughput,
            effective_flow_rate_per_hour=avg_flow,
            score=score,
        )
        if scenario_name == "baseline":
            baseline_replication_tis = list(tis_values)

    if not scenario_outputs:
        return ActionEvaluation(
            action=action,
            predicted_avg_wait_minutes=0.0,
            predicted_avg_time_in_system_minutes=0.0,
            predicted_queue_length=0.0,
            predicted_dock_utilization=0.0,
            predicted_staging_overflow_risk=0.0,
            score=0.0,
            robust_score=0.0,
            scenario_metrics={},
            replication_count=replications,
            replication_avg_tis=[],
            verification={},
        )

    baseline = scenario_outputs.get("baseline", next(iter(scenario_outputs.values())))
    robust_score = max(metric.score for metric in scenario_outputs.values())
    verification = build_verification_bundle(
        throughput_rate_trucks_per_min=max(float(baseline.effective_flow_rate_per_hour), 0.0) / 60.0,
        avg_time_in_system_minutes=baseline.predicted_avg_time_in_system_minutes,
        avg_number_in_system=baseline.predicted_avg_number_in_system,
        replication_means=baseline_replication_tis,
        littles_law_threshold=config.verification_littles_law_threshold,
        ci_threshold=config.verification_ci_ratio_threshold,
    )

    return ActionEvaluation(
        action=action,
        predicted_avg_wait_minutes=baseline.predicted_avg_wait_minutes,
        predicted_avg_time_in_system_minutes=baseline.predicted_avg_time_in_system_minutes,
        predicted_queue_length=baseline.predicted_queue_length,
        predicted_dock_utilization=baseline.predicted_dock_utilization,
        predicted_staging_overflow_risk=baseline.predicted_staging_overflow_risk,
        score=baseline.score,
        predicted_avg_number_in_system=baseline.predicted_avg_number_in_system,
        throughput_trucks_per_hour=baseline.throughput_trucks_per_hour,
        effective_flow_rate_per_hour=baseline.effective_flow_rate_per_hour,
        robust_score=robust_score,
        scenario_metrics=scenario_outputs,
        replication_count=replications,
        replication_avg_tis=baseline_replication_tis,
        verification=verification,
    )

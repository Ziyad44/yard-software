"""Recommendation scaffolding with a small candidate-action set."""

from __future__ import annotations

import copy
import random

from .config import YardConfig
from .models import Action, ActionEvaluation, Recommendation, YardState
from .simulation import simulate_horizon


def _assignments_from_state(state: YardState) -> tuple[dict[int, int], dict[int, int]]:
    workers = {dock_id: max(dock.assigned_workers, 0) for dock_id, dock in state.docks.items() if dock.active}
    forklifts = {dock_id: max(dock.assigned_forklifts, 0) for dock_id, dock in state.docks.items() if dock.active}
    return workers, forklifts


def _dock_pressure(dock_id: int, state: YardState) -> float:
    dock = state.docks[dock_id]
    remaining = dock.current_truck.remaining_load_units if dock.current_truck is not None else 0.0
    return remaining + dock.staging.occupancy_units


def _staging_ratio(dock_id: int, state: YardState) -> float:
    return state.docks[dock_id].staging.occupancy_ratio


def _dedupe_actions(actions: list[Action]) -> list[Action]:
    unique: list[Action] = []
    seen: set[tuple] = set()
    for action in actions:
        key = (
            action.action_name,
            tuple(sorted(action.workers_by_dock.items())),
            tuple(sorted(action.forklifts_by_dock.items())),
            action.hold_gate_release,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(action)
    return unique


def build_candidate_actions(state: YardState, config: YardConfig) -> list[Action]:
    """Create the minimal version-1 candidate set."""
    if not state.docks:
        return []

    workers, forklifts = _assignments_from_state(state)
    if not workers:
        return []
    dock_ids = sorted(workers.keys())
    pressures = {dock_id: _dock_pressure(dock_id, state) for dock_id in dock_ids}
    ratios = {dock_id: _staging_ratio(dock_id, state) for dock_id in dock_ids}

    keep_current = Action(
        action_name="keep_current_plan",
        workers_by_dock=dict(workers),
        forklifts_by_dock=dict(forklifts),
        hold_gate_release=state.hold_gate_release,
        notes="No reassignment. Maintain current operating plan.",
    )
    actions: list[Action] = [keep_current]

    target_by_pressure = max(pressures, key=pressures.get)
    target_by_ratio = max(ratios, key=ratios.get)
    idle_workers = max(state.resources.idle_workers, 0)
    idle_forklifts = max(state.resources.idle_forklifts, 0)

    # Shift one worker to the most loaded dock.
    shifted_workers = dict(workers)
    worker_source = next(
        (dock_id for dock_id in sorted(dock_ids, key=lambda d: workers[d], reverse=True) if dock_id != target_by_pressure and workers[dock_id] > 0),
        None,
    )
    if shifted_workers[target_by_pressure] < config.max_unloaders_per_dock:
        if worker_source is not None:
            shifted_workers[worker_source] -= 1
            shifted_workers[target_by_pressure] += 1
            worker_note = f"Shift one worker from Dock {worker_source} to Dock {target_by_pressure}."
        elif idle_workers > 0:
            shifted_workers[target_by_pressure] += 1
            worker_note = f"Allocate one idle worker to Dock {target_by_pressure}."
        else:
            worker_note = ""
    else:
        worker_note = ""

    if worker_note:
        actions.append(
            Action(
                action_name="shift_one_worker_to_most_loaded_dock",
                workers_by_dock=shifted_workers,
                forklifts_by_dock=dict(forklifts),
                hold_gate_release=False,
                notes=worker_note,
            )
        )

    # Shift one forklift to the most loaded dock.
    shifted_forklifts = dict(forklifts)
    forklift_source = next(
        (dock_id for dock_id in sorted(dock_ids, key=lambda d: forklifts[d], reverse=True) if dock_id != target_by_pressure and forklifts[dock_id] > 0),
        None,
    )
    if forklift_source is not None:
        shifted_forklifts[forklift_source] -= 1
        shifted_forklifts[target_by_pressure] += 1
        forklift_note = f"Shift one forklift from Dock {forklift_source} to Dock {target_by_pressure}."
    elif idle_forklifts > 0:
        shifted_forklifts[target_by_pressure] += 1
        forklift_note = f"Allocate one idle forklift to Dock {target_by_pressure}."
    else:
        forklift_note = ""

    if forklift_note:
        actions.append(
            Action(
                action_name="shift_one_forklift_to_most_loaded_dock",
                workers_by_dock=dict(workers),
                forklifts_by_dock=shifted_forklifts,
                hold_gate_release=False,
                notes=forklift_note,
            )
        )

    # Prioritize clearing at riskiest staging dock, optionally with gate hold.
    clear_workers = dict(workers)
    clear_forklifts = dict(forklifts)
    clear_worker_source = next(
        (dock_id for dock_id in sorted(dock_ids, key=lambda d: workers[d], reverse=True) if dock_id != target_by_ratio and workers[dock_id] > 0),
        None,
    )
    clear_forklift_source = next(
        (dock_id for dock_id in sorted(dock_ids, key=lambda d: forklifts[d], reverse=True) if dock_id != target_by_ratio and forklifts[dock_id] > 0),
        None,
    )
    if clear_worker_source is not None and clear_workers[target_by_ratio] < config.max_unloaders_per_dock:
        clear_workers[clear_worker_source] -= 1
        clear_workers[target_by_ratio] += 1
    elif idle_workers > 0 and clear_workers[target_by_ratio] < config.max_unloaders_per_dock:
        clear_workers[target_by_ratio] += 1
    if clear_forklift_source is not None:
        clear_forklifts[clear_forklift_source] -= 1
        clear_forklifts[target_by_ratio] += 1
    elif idle_forklifts > 0:
        clear_forklifts[target_by_ratio] += 1
    actions.append(
        Action(
            action_name="prioritize_clearing_at_riskiest_dock",
            workers_by_dock=clear_workers,
            forklifts_by_dock=clear_forklifts,
            hold_gate_release=ratios[target_by_ratio] >= state.docks[target_by_ratio].staging.threshold_high,
            notes=f"Prioritize staging clearing at Dock {target_by_ratio}.",
        )
    )

    return _dedupe_actions(actions)


def _apply_action_assignments(state: YardState, action: Action) -> None:
    for dock_id, dock in state.docks.items():
        if not dock.active:
            continue
        dock.assigned_workers = max(int(action.workers_by_dock.get(dock_id, 0)), 0)
        dock.assigned_forklifts = max(int(action.forklifts_by_dock.get(dock_id, 0)), 0)
    state.hold_gate_release = bool(action.hold_gate_release)
    state.update_resource_assignment_counters()


def _score_evaluation(evaluation: ActionEvaluation) -> float:
    """
    Weighted score for candidate comparison.

    Lower is better.
    """
    return (
        1.0 * evaluation.predicted_avg_wait_minutes
        + 0.6 * evaluation.predicted_avg_time_in_system_minutes
        + 0.3 * evaluation.predicted_queue_length
        + 25.0 * evaluation.predicted_staging_overflow_risk
    )


def evaluate_candidates(state: YardState, config: YardConfig, rng_seed: int = 7) -> list[ActionEvaluation]:
    """
    Simulate near-term outcomes for each candidate action.

    Phase-1 note:
    The horizon metrics are intentionally lightweight and will be strengthened in Phase 2.
    """
    evaluations: list[ActionEvaluation] = []
    candidates = build_candidate_actions(state, config)

    for index, action in enumerate(candidates):
        scenario_state = copy.deepcopy(state)
        _apply_action_assignments(scenario_state, action)
        snapshot = simulate_horizon(
            scenario_state,
            config=config,
            minutes=config.lookahead_horizon_minutes,
            rng=random.Random(rng_seed + index),
        )

        evaluation = ActionEvaluation(
            action=action,
            predicted_avg_wait_minutes=float(snapshot.predicted_avg_wait_minutes or 0.0),
            predicted_avg_time_in_system_minutes=float(snapshot.predicted_avg_time_in_system_minutes or 0.0),
            predicted_queue_length=float(snapshot.queue_length),
            predicted_dock_utilization=float(snapshot.predicted_dock_utilization or 0.0),
            predicted_staging_overflow_risk=float(snapshot.predicted_staging_overflow_risk or 0.0),
            score=0.0,
        )
        evaluation.score = _score_evaluation(evaluation)
        evaluations.append(evaluation)

    return evaluations


def _action_to_plain_language(action: Action) -> str:
    if action.action_name == "keep_current_plan":
        return "Keep current plan."
    if action.action_name == "shift_one_worker_to_most_loaded_dock":
        return "Assign one additional worker to the most loaded dock."
    if action.action_name == "shift_one_forklift_to_most_loaded_dock":
        return "Assign one additional forklift to the most loaded dock."
    if action.action_name == "prioritize_clearing_at_riskiest_dock":
        if action.hold_gate_release:
            return "Prioritize clearing at the riskiest dock and hold next gate release."
        return "Prioritize clearing at the riskiest dock."
    return action.notes or action.action_name


def recommend_best_action(state: YardState, config: YardConfig) -> Recommendation | None:
    """Return a recommendation if at least one candidate can be evaluated."""
    evaluations = evaluate_candidates(state, config=config)
    if not evaluations:
        return None

    evaluations.sort(key=lambda item: item.score)
    best = evaluations[0]
    chosen = best

    current_plan_eval = next(
        (item for item in evaluations if item.action.action_name == "keep_current_plan"),
        None,
    )
    if current_plan_eval is not None and best.action.action_name != "keep_current_plan":
        if current_plan_eval.score > 0.0:
            relative_improvement = (current_plan_eval.score - best.score) / current_plan_eval.score
            if relative_improvement < config.min_score_improvement_to_switch:
                chosen = current_plan_eval
        else:
            chosen = current_plan_eval

    candidate_scores = {item.action.action_name: item.score for item in evaluations}
    rationale = _action_to_plain_language(chosen.action)

    return Recommendation(
        selected_action=chosen.action,
        rationale=rationale,
        score=chosen.score,
        candidate_scores=candidate_scores,
    )

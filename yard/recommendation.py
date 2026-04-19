"""Recommendation scaffolding with a small candidate-action set."""

from __future__ import annotations

from .config import YardConfig
from .evaluation import evaluate_action_across_scenarios
from .forecasting import build_forecast
from .models import Action, ActionEvaluation, ForecastResult, Recommendation, TriggerEvent, YardState
from .simulation import dock_load_family, sanitize_assignment_for_dock


def _assignments_from_state(state: YardState, config: YardConfig) -> tuple[dict[int, int], dict[int, int]]:
    workers: dict[int, int] = {}
    forklifts: dict[int, int] = {}
    for dock_id, dock in state.docks.items():
        if not dock.active:
            continue
        normalized_workers, normalized_forklifts = sanitize_assignment_for_dock(
            dock=dock,
            workers=dock.assigned_workers,
            forklifts=dock.assigned_forklifts,
            max_unloaders_per_dock=config.max_unloaders_per_dock,
        )
        workers[dock_id] = normalized_workers
        forklifts[dock_id] = normalized_forklifts
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


DELTA_METRIC_KEYS = (
    "predicted_avg_wait_minutes",
    "predicted_avg_time_in_system_minutes",
    "predicted_queue_length",
    "predicted_dock_utilization",
    "predicted_staging_overflow_risk",
    "throughput_trucks_per_hour",
    "effective_flow_rate_per_hour",
)


def _find_donor_dock(
    *,
    assignments: dict[int, int],
    candidate_dock_ids: list[int],
    target_dock_id: int,
) -> int | None:
    return next(
        (
            dock_id
            for dock_id in sorted(candidate_dock_ids, key=lambda dock: assignments.get(dock, 0), reverse=True)
            if dock_id != target_dock_id and assignments.get(dock_id, 0) > 0
        ),
        None,
    )


def _apply_idle_first_increment(
    *,
    assignments: dict[int, int],
    target_dock_id: int,
    candidate_dock_ids: list[int],
    idle_count: int,
    max_per_dock: int | None = None,
) -> tuple[dict[int, int], str]:
    """
    Allocate one unit of a resource using idle-first policy.

    Returns:
    - updated assignments
    - source tag: "idle_pool", "shifted_from_dock_<id>", or ""
    """
    updated = dict(assignments)
    if target_dock_id not in updated:
        return updated, ""

    if max_per_dock is not None and updated[target_dock_id] >= max(max_per_dock, 0):
        return updated, ""

    if idle_count > 0:
        updated[target_dock_id] += 1
        return updated, "idle_pool"

    donor_dock_id = _find_donor_dock(
        assignments=updated,
        candidate_dock_ids=candidate_dock_ids,
        target_dock_id=target_dock_id,
    )
    if donor_dock_id is None:
        return updated, ""

    updated[donor_dock_id] -= 1
    updated[target_dock_id] += 1
    return updated, f"shifted_from_dock_{donor_dock_id}"


def _resource_note(*, resource_name: str, target_dock_id: int, source_tag: str) -> str:
    if source_tag == "idle_pool":
        return (
            f"Allocate one idle {resource_name} to Dock {target_dock_id} "
            f"(source=idle_pool)."
        )
    if source_tag.startswith("shifted_from_dock_"):
        donor = source_tag.removeprefix("shifted_from_dock_")
        return (
            f"Shift one {resource_name} from Dock {donor} to Dock {target_dock_id} "
            f"(source={source_tag})."
        )
    return ""


def build_candidate_actions(state: YardState, config: YardConfig) -> list[Action]:
    """Create the minimal version-1 candidate set."""
    if not state.docks:
        return []

    workers, forklifts = _assignments_from_state(state, config=config)
    if not workers:
        return []
    dock_ids = sorted(workers.keys())
    pressures = {dock_id: _dock_pressure(dock_id, state) for dock_id in dock_ids}
    ratios = {dock_id: _staging_ratio(dock_id, state) for dock_id in dock_ids}
    floor_dock_ids = [dock_id for dock_id in dock_ids if dock_load_family(state.docks[dock_id]) == "floor"]
    pallet_dock_ids = [dock_id for dock_id in dock_ids if dock_load_family(state.docks[dock_id]) == "palletized"]

    keep_current = Action(
        action_name="keep_current_plan",
        workers_by_dock=dict(workers),
        forklifts_by_dock=dict(forklifts),
        hold_gate_release=state.hold_gate_release,
        notes="No reassignment. Maintain current operating plan.",
    )
    actions: list[Action] = [keep_current]

    idle_workers = max(state.resources.idle_workers, 0)
    idle_forklifts = max(state.resources.idle_forklifts, 0)

    # Shift one worker to the most loaded floor-handling dock.
    if floor_dock_ids:
        target_by_floor_pressure = max(floor_dock_ids, key=pressures.get)
        shifted_workers, worker_source_tag = _apply_idle_first_increment(
            assignments=workers,
            target_dock_id=target_by_floor_pressure,
            candidate_dock_ids=floor_dock_ids,
            idle_count=idle_workers,
            max_per_dock=config.max_unloaders_per_dock,
        )
        worker_note = _resource_note(
            resource_name="worker",
            target_dock_id=target_by_floor_pressure,
            source_tag=worker_source_tag,
        )

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

    # Shift one forklift to the most loaded palletized-handling dock.
    if pallet_dock_ids:
        target_by_pallet_pressure = max(pallet_dock_ids, key=pressures.get)
        shifted_forklifts, forklift_source_tag = _apply_idle_first_increment(
            assignments=forklifts,
            target_dock_id=target_by_pallet_pressure,
            candidate_dock_ids=pallet_dock_ids,
            idle_count=idle_forklifts,
        )
        forklift_note = _resource_note(
            resource_name="forklift",
            target_dock_id=target_by_pallet_pressure,
            source_tag=forklift_source_tag,
        )

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
    target_by_ratio = max(ratios, key=ratios.get)
    target_family = dock_load_family(state.docks[target_by_ratio])
    clear_note = f"Prioritize staging clearing at Dock {target_by_ratio}."
    if target_family == "floor":
        clear_workers, clear_source_tag = _apply_idle_first_increment(
            assignments=clear_workers,
            target_dock_id=target_by_ratio,
            candidate_dock_ids=floor_dock_ids,
            idle_count=idle_workers,
            max_per_dock=config.max_unloaders_per_dock,
        )
        resource_note = _resource_note(
            resource_name="worker",
            target_dock_id=target_by_ratio,
            source_tag=clear_source_tag,
        )
        if resource_note:
            clear_note = f"{clear_note} {resource_note}"
        else:
            clear_note = f"{clear_note} Keep current worker split (source=no_change)."
    elif target_family == "palletized":
        clear_forklifts, clear_source_tag = _apply_idle_first_increment(
            assignments=clear_forklifts,
            target_dock_id=target_by_ratio,
            candidate_dock_ids=pallet_dock_ids,
            idle_count=idle_forklifts,
        )
        resource_note = _resource_note(
            resource_name="forklift",
            target_dock_id=target_by_ratio,
            source_tag=clear_source_tag,
        )
        if resource_note:
            clear_note = f"{clear_note} {resource_note}"
        else:
            clear_note = f"{clear_note} Keep current forklift split (source=no_change)."
    else:
        clear_note = f"{clear_note} No clear load family active (source=no_change)."
    actions.append(
        Action(
            action_name="prioritize_clearing_at_riskiest_dock",
            workers_by_dock=clear_workers,
            forklifts_by_dock=clear_forklifts,
            hold_gate_release=ratios[target_by_ratio] >= state.docks[target_by_ratio].staging.threshold_high,
            notes=clear_note,
        )
    )

    return _dedupe_actions(actions)


def _effective_score(evaluation: ActionEvaluation) -> float:
    if evaluation.robust_score > 0.0:
        return evaluation.robust_score
    return evaluation.score


def _is_equivalent_to_live_plan(action: Action, state: YardState) -> bool:
    if bool(action.hold_gate_release) != bool(state.hold_gate_release):
        return False
    for dock_id, dock in state.docks.items():
        if not dock.active:
            continue
        if int(action.workers_by_dock.get(dock_id, 0)) != int(dock.assigned_workers):
            return False
        if int(action.forklifts_by_dock.get(dock_id, 0)) != int(dock.assigned_forklifts):
            return False
    return True


def evaluate_candidates(
    state: YardState,
    config: YardConfig,
    rng_seed: int = 7,
    forecast: ForecastResult | None = None,
) -> list[ActionEvaluation]:
    """
    Simulate near-term outcomes for each candidate action.

    Evaluate each candidate under low/baseline/high scenarios with replications.
    """
    evaluations: list[ActionEvaluation] = []
    candidates = build_candidate_actions(state, config)
    forecast_result = forecast if forecast is not None else build_forecast(state, config)
    scenario_rates = forecast_result.scenarios or {"baseline": config.arrival_rate_per_hour}

    for index, action in enumerate(candidates):
        evaluation = evaluate_action_across_scenarios(
            state=state,
            action=action,
            config=config,
            scenario_rates=scenario_rates,
            rng_seed=rng_seed + index * 31,
        )
        if evaluation.robust_score <= 0.0:
            evaluation.robust_score = evaluation.score
        evaluations.append(evaluation)

    return evaluations


def _evaluation_baseline_metrics(evaluation: ActionEvaluation) -> dict[str, float]:
    baseline_metrics = {
        "predicted_avg_wait_minutes": evaluation.predicted_avg_wait_minutes,
        "predicted_avg_time_in_system_minutes": evaluation.predicted_avg_time_in_system_minutes,
        "predicted_queue_length": evaluation.predicted_queue_length,
        "predicted_avg_number_in_system": evaluation.predicted_avg_number_in_system,
        "predicted_dock_utilization": evaluation.predicted_dock_utilization,
        "predicted_staging_overflow_risk": evaluation.predicted_staging_overflow_risk,
        "throughput_trucks_per_hour": evaluation.throughput_trucks_per_hour,
        "effective_flow_rate_per_hour": evaluation.effective_flow_rate_per_hour,
    }
    scenario_baseline = evaluation.scenario_metrics.get("baseline")
    if scenario_baseline is not None:
        baseline_metrics["scenario_score"] = scenario_baseline.score
    return baseline_metrics


def _infer_target_dock(action: Action, state: YardState) -> int | None:
    active_dock_ids = sorted(dock_id for dock_id, dock in state.docks.items() if dock.active)
    if action.action_name == "keep_current_plan" or not active_dock_ids:
        return None

    if action.action_name == "shift_one_worker_to_most_loaded_dock":
        floor_dock_ids = [dock_id for dock_id in active_dock_ids if dock_load_family(state.docks[dock_id]) == "floor"]
        if floor_dock_ids:
            return max(floor_dock_ids, key=lambda dock_id: _dock_pressure(dock_id, state))
    elif action.action_name == "shift_one_forklift_to_most_loaded_dock":
        pallet_dock_ids = [
            dock_id for dock_id in active_dock_ids if dock_load_family(state.docks[dock_id]) == "palletized"
        ]
        if pallet_dock_ids:
            return max(pallet_dock_ids, key=lambda dock_id: _dock_pressure(dock_id, state))
    elif action.action_name == "prioritize_clearing_at_riskiest_dock":
        return max(active_dock_ids, key=lambda dock_id: _staging_ratio(dock_id, state))

    best_dock: int | None = None
    best_gain = 0
    for dock_id in active_dock_ids:
        worker_gain = int(action.workers_by_dock.get(dock_id, 0)) - int(state.docks[dock_id].assigned_workers)
        forklift_gain = int(action.forklifts_by_dock.get(dock_id, 0)) - int(state.docks[dock_id].assigned_forklifts)
        gain = max(worker_gain, 0) + max(forklift_gain, 0)
        if gain > best_gain:
            best_gain = gain
            best_dock = dock_id
    return best_dock


def _selected_dock_reason(action: Action, state: YardState, target_dock_id: int | None) -> str:
    if target_dock_id is None:
        return "No dock-specific reassignment is required; keep the current plan."

    pressure = _dock_pressure(target_dock_id, state)
    staging_pct = 100.0 * _staging_ratio(target_dock_id, state)
    if action.action_name in (
        "shift_one_worker_to_most_loaded_dock",
        "shift_one_forklift_to_most_loaded_dock",
    ):
        return (
            f"Dock {target_dock_id} selected because it has the highest combined remaining load and staging pressure "
            f"({pressure:.1f} units)."
        )
    if action.action_name == "prioritize_clearing_at_riskiest_dock":
        return (
            f"Dock {target_dock_id} selected because it has the highest staging occupancy pressure "
            f"({staging_pct:.1f}% full)."
        )
    return f"Dock {target_dock_id} selected as the highest-impact reassignment target."


def _resource_source_reason(action: Action, target_dock_id: int | None) -> str:
    lower_notes = action.notes.lower()
    resource_name = "resource"
    if "worker" in lower_notes or "worker" in action.action_name:
        resource_name = "worker"
    elif "forklift" in lower_notes or "forklift" in action.action_name:
        resource_name = "forklift"

    if "source=idle_pool" in lower_notes:
        if target_dock_id is not None:
            return f"One idle {resource_name} is allocated to Dock {target_dock_id} (source: idle pool)."
        return f"One idle {resource_name} is allocated from the idle pool."
    if "source=shifted_from_dock_" in lower_notes:
        marker = "source=shifted_from_dock_"
        source_suffix = lower_notes.split(marker, maxsplit=1)[1]
        source_dock = source_suffix.split(")", maxsplit=1)[0].strip()
        if target_dock_id is not None:
            return (
                f"One {resource_name} is shifted from Dock {source_dock} to Dock {target_dock_id} "
                "(source: reassignment)."
            )
        return f"One {resource_name} is shifted from Dock {source_dock} (source: reassignment)."
    if action.action_name == "keep_current_plan":
        return "No resource movement is recommended; current assignment remains active."
    return "No additional idle resources are available for this change."


def _build_kpi_delta(
    *,
    selected: ActionEvaluation,
    current_plan: ActionEvaluation | None,
) -> dict[str, dict[str, float]]:
    selected_metrics = _evaluation_baseline_metrics(selected)
    current_metrics = _evaluation_baseline_metrics(current_plan) if current_plan is not None else selected_metrics

    deltas: dict[str, dict[str, float]] = {}
    for metric_name in DELTA_METRIC_KEYS:
        before_value = float(current_metrics.get(metric_name, 0.0))
        after_value = float(selected_metrics.get(metric_name, 0.0))
        deltas[metric_name] = {
            "before": round(before_value, 3),
            "after": round(after_value, 3),
            "delta": round(after_value - before_value, 3),
        }
    return deltas


def _wait_improvement_sentence(kpi_delta: dict[str, dict[str, float]]) -> str:
    wait_delta = kpi_delta.get("predicted_avg_wait_minutes", {})
    before = float(wait_delta.get("before", 0.0))
    after = float(wait_delta.get("after", 0.0))
    if abs(after - before) <= 1e-6:
        return f"Predicted wait remains {after:.1f} minutes."
    if after < before:
        return f"Predicted wait improves from {before:.1f} to {after:.1f} minutes."
    return f"Predicted wait increases from {before:.1f} to {after:.1f} minutes."


def _compose_rationale(
    *,
    trigger_reason: str,
    dock_reason: str,
    source_reason: str,
    wait_sentence: str,
) -> str:
    parts = []
    if trigger_reason:
        parts.append(trigger_reason.strip())
    if dock_reason:
        parts.append(dock_reason.strip())
    if source_reason:
        parts.append(source_reason.strip())
    if wait_sentence:
        parts.append(wait_sentence.strip())
    return " ".join(part for part in parts if part)


def recommend_best_action(
    state: YardState,
    config: YardConfig,
    trigger_event: TriggerEvent | None = None,
) -> Recommendation | None:
    """Return a recommendation if at least one candidate can be evaluated."""
    forecast = build_forecast(state, config)
    evaluations = evaluate_candidates(state, config=config, forecast=forecast)
    if not evaluations:
        return None

    evaluations.sort(key=_effective_score)
    best = evaluations[0]
    chosen = best

    current_plan_eval = next(
        (item for item in evaluations if item.action.action_name == "keep_current_plan"),
        None,
    )
    selection_note = "Best-scoring candidate selected."

    if current_plan_eval is not None and best.action.action_name != "keep_current_plan":
        current_plan_score = _effective_score(current_plan_eval)
        best_score = _effective_score(best)
        if current_plan_score > 0.0:
            relative_improvement = (current_plan_score - best_score) / current_plan_score
            if relative_improvement < config.min_score_improvement_to_switch:
                chosen = current_plan_eval
                selection_note = (
                    "Best score improvement is below the switch threshold, so the current plan is kept."
                )
        else:
            chosen = current_plan_eval
            selection_note = "Current plan has zero baseline score, so reassignment is not applied."
    if (
        current_plan_eval is not None
        and chosen.action.action_name != "keep_current_plan"
        and _is_equivalent_to_live_plan(chosen.action, state)
    ):
        chosen = current_plan_eval
        selection_note = "Best candidate matches the current live assignment, so no reassignment is needed."
    if chosen.action.action_name == "keep_current_plan" and selection_note == "Best-scoring candidate selected.":
        selection_note = "Current plan remains the best candidate in this cycle."

    candidate_scores = {item.action.action_name: _effective_score(item) for item in evaluations}
    selected_baseline_metrics = _evaluation_baseline_metrics(chosen)
    selected_target_dock_id = _infer_target_dock(chosen.action, state)
    selected_dock_reason = _selected_dock_reason(chosen.action, state, selected_target_dock_id)
    resource_source_reason = _resource_source_reason(chosen.action, selected_target_dock_id)
    kpi_delta = _build_kpi_delta(selected=chosen, current_plan=current_plan_eval)
    rationale = _compose_rationale(
        trigger_reason=trigger_event.reason if trigger_event is not None else "",
        dock_reason=selected_dock_reason,
        source_reason=resource_source_reason,
        wait_sentence=_wait_improvement_sentence(kpi_delta),
    )

    return Recommendation(
        selected_action=chosen.action,
        rationale=rationale,
        score=chosen.score,
        candidate_scores=candidate_scores,
        robust_score=chosen.robust_score,
        forecast=forecast,
        evaluations=evaluations,
        verification=chosen.verification,
        selected_baseline_metrics=selected_baseline_metrics,
        selected_target_dock_id=selected_target_dock_id,
        selected_dock_reason=selected_dock_reason,
        resource_source_reason=resource_source_reason,
        kpi_delta=kpi_delta,
        selection_note=selection_note,
    )

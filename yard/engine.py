"""Top-level engine orchestration for version-1 yard cycle."""

from __future__ import annotations

import random

from .config import YardConfig
from .models import (
    Action,
    DockState,
    DockSummary,
    Recommendation,
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
            "predicted_dock_utilization": float(snapshot.predicted_dock_utilization or 0.0),
            "predicted_staging_overflow_risk": float(snapshot.predicted_staging_overflow_risk or 0.0),
        }
    )


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
    if dock_summaries:
        dock_utilization = sum(1.0 for summary in dock_summaries if summary.current_truck_id) / len(dock_summaries)
        staging_risk = sum(
            1.0
            for summary in dock_summaries
            if summary.staging_capacity_units > 0.0
            and (summary.staging_occupancy_units / summary.staging_capacity_units) >= 0.85
        ) / len(dock_summaries)

    predicted_wait = state.kpi_cache.get("predicted_avg_wait_minutes", float(state.queue_length))
    predicted_tis = state.kpi_cache.get("predicted_avg_time_in_system_minutes", predicted_wait + 20.0)
    predicted_util = state.kpi_cache.get("predicted_dock_utilization", dock_utilization)
    predicted_risk = state.kpi_cache.get("predicted_staging_overflow_risk", staging_risk)

    return SystemSnapshot(
        minute=state.now_minute,
        queue_length=state.queue_length,
        predicted_avg_wait_minutes=predicted_wait,
        predicted_avg_time_in_system_minutes=predicted_tis,
        predicted_dock_utilization=predicted_util,
        predicted_staging_overflow_risk=predicted_risk,
        recommended_action_text=recommendation_text,
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

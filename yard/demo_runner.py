"""CLI demo runner for the Phase 2 version-1 backend."""

from __future__ import annotations

import random

from .config import YardConfig
from .engine import apply_action, initialize_state, run_minute_cycle, snapshot_from_state
from .models import Action


def _initial_balanced_action(state_dock_ids: list[int], config: YardConfig, workers: int, forklifts: int) -> Action:
    workers_by_dock = {dock_id: 0 for dock_id in state_dock_ids}
    forklifts_by_dock = {dock_id: 0 for dock_id in state_dock_ids}

    dock_count = max(len(state_dock_ids), 1)
    for idx in range(workers):
        dock_id = state_dock_ids[idx % dock_count]
        if workers_by_dock[dock_id] < config.max_unloaders_per_dock:
            workers_by_dock[dock_id] += 1
    for idx in range(forklifts):
        dock_id = state_dock_ids[idx % dock_count]
        forklifts_by_dock[dock_id] += 1

    return Action(
        action_name="initial_balanced_assignment",
        workers_by_dock=workers_by_dock,
        forklifts_by_dock=forklifts_by_dock,
        hold_gate_release=False,
        notes="Initial balanced resource placement for demo startup.",
    )


def main() -> None:
    config = YardConfig(
        arrival_rate_per_hour=40.0,
        review_interval_minutes=5,
        lookahead_horizon_minutes=20,
        min_score_improvement_to_switch=0.0,
    )
    state = initialize_state(
        available_workers=3,
        available_forklifts=1,
        active_docks=2,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )

    initial = _initial_balanced_action(
        state_dock_ids=sorted(state.docks.keys()),
        config=config,
        workers=state.resources.total_workers,
        forklifts=state.resources.total_forklifts,
    )
    # Seed an intentionally imbalanced plan so recommendation effects are visible.
    initial.workers_by_dock = {1: 3, 2: 0}
    initial.forklifts_by_dock = {1: 1, 2: 0}
    apply_action(state, initial, config=config)
    state.docks[2].staging.occupancy_units = 92.0

    rng = random.Random(42)
    auto_apply_recommendation = True
    total_minutes = 60

    print("Starting demo run...")
    for _ in range(total_minutes):
        triggers, recommendation = run_minute_cycle(state, config=config, rng=rng)

        applied_text = "-"
        if recommendation and auto_apply_recommendation:
            apply_action(state, recommendation.selected_action, config=config)
            applied_text = recommendation.selected_action.action_name

        snapshot = snapshot_from_state(state)
        trigger_names = ",".join(t.trigger_type for t in triggers) if triggers else "-"
        recommendation_text = recommendation.rationale if recommendation else "-"
        staging_by_dock = ", ".join(
            f"D{dock.dock_id}:{dock.staging_occupancy_units:.1f}" for dock in snapshot.docks
        )
        print(
            f"t={snapshot.minute:03d} | queue={snapshot.queue_length:02d} | "
            f"wait_pred={snapshot.predicted_avg_wait_minutes:.2f} | "
            f"staging_risk={snapshot.predicted_staging_overflow_risk:.2f} | "
            f"staging=[{staging_by_dock}] | triggers={trigger_names} | "
            f"rec={recommendation_text} | applied={applied_text}"
        )

    print("Demo completed.")


if __name__ == "__main__":
    main()

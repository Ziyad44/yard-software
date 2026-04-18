import random

from yard.config import YardConfig
from yard.engine import apply_action, initialize_state, run_minute_cycle, snapshot_from_state
from yard.models import Action, Truck


def test_applied_action_changes_subsequent_behavior() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0, review_interval_minutes=30)
    state = initialize_state(
        available_workers=2,
        available_forklifts=1,
        active_docks=1,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    state.waiting_queue.append(
        Truck(
            truck_id="T00100",
            truck_type="small_floor",
            initial_load_units=30.0,
            remaining_load_units=30.0,
            gate_arrival_minute=0,
        )
    )

    hold_action = Action(
        action_name="hold_release",
        workers_by_dock={1: 1},
        forklifts_by_dock={1: 0},
        hold_gate_release=True,
    )
    apply_action(state, hold_action, config=config)
    run_minute_cycle(state, config=config, rng=random.Random(1))
    assert state.queue_length == 1
    assert state.docks[1].current_truck is None

    release_action = Action(
        action_name="release",
        workers_by_dock={1: 1},
        forklifts_by_dock={1: 0},
        hold_gate_release=False,
    )
    apply_action(state, release_action, config=config)
    run_minute_cycle(state, config=config, rng=random.Random(2))
    assert state.queue_length == 0
    assert state.docks[1].current_truck is not None


def test_recommendation_and_live_state_consistency() -> None:
    config = YardConfig(arrival_rate_per_hour=0.0, review_interval_minutes=2, lookahead_horizon_minutes=5)
    state = initialize_state(
        available_workers=4,
        available_forklifts=2,
        active_docks=2,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    state.waiting_queue.append(
        Truck(
            truck_id="T00101",
            truck_type="medium_floor",
            initial_load_units=50.0,
            remaining_load_units=50.0,
            gate_arrival_minute=0,
        )
    )

    triggers1, recommendation1 = run_minute_cycle(state, config=config, rng=random.Random(7))
    assert not triggers1
    assert recommendation1 is None
    assert state.last_recommendation is None

    triggers2, recommendation2 = run_minute_cycle(state, config=config, rng=random.Random(8))
    assert any(t.trigger_type == "review_timer" for t in triggers2)
    assert recommendation2 is not None
    assert state.last_recommendation is recommendation2

    apply_action(state, recommendation2.selected_action, config=config)
    applied_workers = {dock_id: dock.assigned_workers for dock_id, dock in state.docks.items()}
    applied_forklifts = {dock_id: dock.assigned_forklifts for dock_id, dock in state.docks.items()}

    run_minute_cycle(state, config=config, rng=random.Random(9))
    assert {dock_id: dock.assigned_workers for dock_id, dock in state.docks.items()} == applied_workers
    assert {dock_id: dock.assigned_forklifts for dock_id, dock in state.docks.items()} == applied_forklifts

    snapshot = snapshot_from_state(state)
    assert snapshot.predicted_avg_wait_minutes is not None
    assert snapshot.predicted_avg_time_in_system_minutes is not None
    assert snapshot.predicted_dock_utilization is not None
    assert snapshot.predicted_staging_overflow_risk is not None
    assert snapshot.recommended_action_text is not None

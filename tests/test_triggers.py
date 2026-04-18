import pytest

from yard.config import YardConfig
from yard.engine import initialize_state
from yard.models import DockState, StagingAreaState
from yard.simulation import detect_review_timer_event, detect_staging_threshold_event


def test_staging_threshold_fires_once_above_high_then_hysteresis_reset() -> None:
    dock = DockState(
        dock_id=1,
        staging=StagingAreaState(
            dock_id=1,
            occupancy_units=0.0,
            capacity_units=100.0,
            threshold_high=0.8,
            threshold_low=0.7,
        ),
    )

    dock.staging.occupancy_units = 81.0
    first = detect_staging_threshold_event(dock, minute=3)
    assert first is not None
    assert first.trigger_type == "staging_threshold"
    assert dock.staging.threshold_alert_active

    dock.staging.occupancy_units = 95.0
    second = detect_staging_threshold_event(dock, minute=4)
    assert second is None
    assert dock.staging.threshold_alert_active

    dock.staging.occupancy_units = 72.0
    near_low = detect_staging_threshold_event(dock, minute=5)
    assert near_low is None
    assert dock.staging.threshold_alert_active

    dock.staging.occupancy_units = 68.0
    reset = detect_staging_threshold_event(dock, minute=6)
    assert reset is None
    assert not dock.staging.threshold_alert_active

    dock.staging.occupancy_units = 83.0
    rearm = detect_staging_threshold_event(dock, minute=7)
    assert rearm is not None
    assert rearm.trigger_type == "staging_threshold"


def test_review_timer_cadence() -> None:
    config = YardConfig(review_interval_minutes=5)
    state = initialize_state(
        available_workers=2,
        available_forklifts=1,
        active_docks=1,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )

    assert detect_review_timer_event(state, minute=4, config=config) is None

    first = detect_review_timer_event(state, minute=5, config=config)
    assert first is not None
    assert first.trigger_type == "review_timer"
    assert state.last_review_minute == 5
    assert state.next_review_minute == 10

    assert detect_review_timer_event(state, minute=9, config=config) is None

    second = detect_review_timer_event(state, minute=10, config=config)
    assert second is not None
    assert second.trigger_type == "review_timer"
    assert state.last_review_minute == 10
    assert state.next_review_minute == 15


def test_review_timer_rejects_non_positive_interval() -> None:
    config = YardConfig(review_interval_minutes=0)
    state = initialize_state(
        available_workers=2,
        available_forklifts=1,
        active_docks=1,
        max_unloaders_per_dock=4,
        config=YardConfig(review_interval_minutes=1),
    )
    state.next_review_minute = 0

    with pytest.raises(ValueError):
        detect_review_timer_event(state, minute=0, config=config)

from yard.config import DEFAULT_STAGING_CAPACITY_UNITS, DEFAULT_TRUCK_LOAD_UNITS, YardConfig
from yard.engine import initialize_state


EXPECTED_TRUCK_LOADS = {
    "small_floor": 6.0,
    "medium_floor": 12.0,
    "large_floor": 30.0,
    "small_palletized": 6.0,
    "medium_palletized": 12.0,
    "large_palletized": 30.0,
}


def test_default_truck_load_mapping_matches_expected_sizes() -> None:
    config = YardConfig()

    assert DEFAULT_TRUCK_LOAD_UNITS == EXPECTED_TRUCK_LOADS
    assert config.resolved_truck_load_units() == EXPECTED_TRUCK_LOADS


def test_resolved_truck_load_mapping_falls_back_for_invalid_or_missing_values() -> None:
    config = YardConfig(
        truck_load_units={
            "small_floor": -1.0,
            "medium_floor": "invalid",
            "large_floor": 30.0,
            "small_palletized": 6.0,
        }
    )

    assert config.resolved_truck_load_units() == EXPECTED_TRUCK_LOADS


def test_default_staging_capacity_is_40_and_propagates_to_initialized_docks() -> None:
    config = YardConfig()
    state = initialize_state(
        available_workers=4,
        available_forklifts=2,
        active_docks=3,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )

    assert DEFAULT_STAGING_CAPACITY_UNITS == 40.0
    assert config.staging_capacity_units == 40.0
    assert all(dock.staging.capacity_units == 40.0 for dock in state.docks.values())


def test_default_verification_thresholds_and_replication_count_are_strict_and_stable() -> None:
    config = YardConfig()

    assert config.verification_littles_law_threshold == 0.10
    assert config.verification_ci_ratio_threshold == 0.20
    assert config.evaluation_replications >= 2

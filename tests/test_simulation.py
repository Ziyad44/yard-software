import random

import pytest

from yard.config import YardConfig
from yard.engine import initialize_state
from yard.models import DockState, StagingAreaState, Truck, YardState, ResourcePool
from yard.simulation import (
    compute_clear_rate,
    compute_unload_rate,
    generate_arrivals_for_minute,
    simulate_one_minute,
    update_busy_dock_one_step,
)


def _empty_state() -> YardState:
    return YardState(now_minute=0, resources=ResourcePool(total_workers=4, total_forklifts=2))


def test_arrival_rate_behavior_low_vs_high() -> None:
    low_cfg = YardConfig(arrival_rate_per_hour=2.0)
    high_cfg = YardConfig(arrival_rate_per_hour=24.0)
    low_state = _empty_state()
    high_state = _empty_state()
    low_rng = random.Random(11)
    high_rng = random.Random(11)

    low_count = 0
    high_count = 0
    for minute in range(240):
        low_state.now_minute = minute
        high_state.now_minute = minute
        low_count += len(generate_arrivals_for_minute(low_state, low_cfg, low_rng))
        high_count += len(generate_arrivals_for_minute(high_state, high_cfg, high_rng))

    assert high_count > low_count
    assert high_count >= 4 * low_count


def test_arrival_mix_sanity() -> None:
    config = YardConfig(arrival_rate_per_hour=120.0)
    state = _empty_state()
    rng = random.Random(29)

    counts = {truck_type: 0 for truck_type in config.normalized_truck_type_mix()}
    total = 0
    for minute in range(1500):
        state.now_minute = minute
        arrivals = generate_arrivals_for_minute(state, config, rng)
        for truck in arrivals:
            counts[truck.truck_type] += 1
        total += len(arrivals)

    assert total > 0
    observed_mix = {k: v / total for k, v in counts.items()}
    expected_mix = config.normalized_truck_type_mix()

    for truck_type, expected_prob in expected_mix.items():
        assert abs(observed_mix[truck_type] - expected_prob) < 0.04


def test_arrivals_are_deterministic_with_fixed_seed() -> None:
    config = YardConfig(arrival_rate_per_hour=18.0)

    def run(seed: int) -> list[tuple[str, str, int]]:
        state = _empty_state()
        rng = random.Random(seed)
        records: list[tuple[str, str, int]] = []
        for minute in range(120):
            state.now_minute = minute
            arrivals = generate_arrivals_for_minute(state, config, rng)
            records.extend((truck.truck_id, truck.truck_type, truck.gate_arrival_minute) for truck in arrivals)
        return records

    assert run(44) == run(44)
    assert run(44) != run(45)


def test_unload_rate_is_exclusive_by_load_type() -> None:
    config = YardConfig()
    floor_truck = Truck(
        truck_id="T-FLOOR",
        truck_type="medium_floor",
        initial_load_units=50.0,
        remaining_load_units=50.0,
        gate_arrival_minute=0,
    )
    pallet_truck = Truck(
        truck_id="T-PALLET",
        truck_type="medium_palletized",
        initial_load_units=40.0,
        remaining_load_units=40.0,
        gate_arrival_minute=0,
    )
    dock = DockState(
        dock_id=1,
        active=True,
        assigned_workers=2,
        assigned_forklifts=3,
        staging=StagingAreaState(dock_id=1, occupancy_units=0.0, capacity_units=100.0),
    )

    assert compute_unload_rate(floor_truck, dock, config) == pytest.approx(
        config.floor_unload_worker_rate * 2
    )
    assert compute_unload_rate(pallet_truck, dock, config) == pytest.approx(
        config.pallet_unload_forklift_rate * 3
    )


def test_clear_rate_is_exclusive_by_load_type() -> None:
    config = YardConfig(clear_worker_rate=1.1, clear_forklift_rate=2.4)
    floor_truck = Truck(
        truck_id="T-FLOOR-CLEAR",
        truck_type="small_floor",
        initial_load_units=30.0,
        remaining_load_units=5.0,
        gate_arrival_minute=0,
    )
    pallet_truck = Truck(
        truck_id="T-PALLET-CLEAR",
        truck_type="small_palletized",
        initial_load_units=24.0,
        remaining_load_units=5.0,
        gate_arrival_minute=0,
    )
    floor_dock = DockState(
        dock_id=1,
        active=True,
        current_truck=floor_truck,
        assigned_workers=3,
        assigned_forklifts=2,
        staging=StagingAreaState(dock_id=1, occupancy_units=10.0, capacity_units=100.0),
    )
    pallet_dock = DockState(
        dock_id=2,
        active=True,
        current_truck=pallet_truck,
        assigned_workers=3,
        assigned_forklifts=2,
        staging=StagingAreaState(dock_id=2, occupancy_units=10.0, capacity_units=100.0),
    )

    assert compute_clear_rate(floor_dock, config) == pytest.approx(config.clear_worker_rate * 3)
    assert compute_clear_rate(pallet_dock, config) == pytest.approx(config.clear_forklift_rate * 2)


def test_flow_conservation_equations_hold() -> None:
    config = YardConfig()
    truck = Truck(
        truck_id="T00010",
        truck_type="medium_floor",
        initial_load_units=50.0,
        remaining_load_units=34.0,
        gate_arrival_minute=0,
    )
    dock = DockState(
        dock_id=1,
        active=True,
        current_truck=truck,
        assigned_workers=2,
        assigned_forklifts=1,
        staging=StagingAreaState(dock_id=1, occupancy_units=20.0, capacity_units=100.0),
    )

    previous_staging = dock.staging.occupancy_units
    previous_remaining = truck.remaining_load_units
    unload_rate = compute_unload_rate(truck, dock, config)
    clear_rate = compute_clear_rate(dock, config)
    dt = config.time_step_minutes
    headroom = 100.0 - previous_staging
    inflow = min(unload_rate * dt, previous_remaining, max(headroom, 0.0))
    outflow = min(clear_rate * dt, previous_staging + inflow)

    was_freed = update_busy_dock_one_step(dock, config=config)

    assert not was_freed
    assert dock.staging.occupancy_units == pytest.approx(previous_staging + inflow - outflow, abs=1e-6)
    assert truck.remaining_load_units == pytest.approx(max(previous_remaining - inflow, 0.0), abs=1e-6)


def test_unloading_pauses_when_staging_is_full() -> None:
    config = YardConfig()
    truck = Truck(
        truck_id="T00011",
        truck_type="medium_floor",
        initial_load_units=50.0,
        remaining_load_units=10.0,
        gate_arrival_minute=0,
    )
    dock = DockState(
        dock_id=1,
        active=True,
        current_truck=truck,
        assigned_workers=2,
        assigned_forklifts=0,
        staging=StagingAreaState(dock_id=1, occupancy_units=100.0, capacity_units=100.0),
    )

    was_freed = update_busy_dock_one_step(dock, config=config)

    assert not was_freed
    assert truck.remaining_load_units == pytest.approx(10.0)
    assert dock.staging.occupancy_units < 100.0


def test_no_early_dock_release_while_staging_has_load() -> None:
    config = YardConfig(clear_worker_rate=0.0, clear_forklift_rate=0.0)
    truck = Truck(
        truck_id="T00012",
        truck_type="small_floor",
        initial_load_units=20.0,
        remaining_load_units=1.0,
        gate_arrival_minute=0,
    )
    dock = DockState(
        dock_id=1,
        active=True,
        current_truck=truck,
        assigned_workers=2,
        assigned_forklifts=0,
        staging=StagingAreaState(dock_id=1, occupancy_units=40.0, capacity_units=100.0),
    )

    was_freed = update_busy_dock_one_step(dock, config=config)

    assert not was_freed
    assert truck.remaining_load_units == pytest.approx(0.0)
    assert dock.staging.occupancy_units > 0.0
    assert dock.current_truck is not None


def test_arrivals_feed_waiting_queue() -> None:
    config = YardConfig(arrival_rate_per_hour=60.0)
    state = initialize_state(
        available_workers=2,
        available_forklifts=1,
        active_docks=1,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    rng = random.Random(5)

    baseline = state.queue_length
    for _ in range(10):
        state.now_minute += 1
        generate_arrivals_for_minute(state, config, rng)

    assert state.queue_length >= baseline


def test_completion_records_departure_and_gate_history() -> None:
    config = YardConfig(
        arrival_rate_per_hour=0.0,
        review_interval_minutes=999,
        floor_unload_worker_rate=4.0,
        clear_worker_rate=4.0,
        clear_forklift_rate=0.0,
    )
    state = initialize_state(
        available_workers=1,
        available_forklifts=0,
        active_docks=1,
        max_unloaders_per_dock=config.max_unloaders_per_dock,
        config=config,
    )
    dock = state.docks[1]
    dock.current_truck = Truck(
        truck_id="T-HIST-1",
        truck_type="small_floor",
        initial_load_units=4.0,
        remaining_load_units=4.0,
        gate_arrival_minute=0,
        assigned_dock_id=1,
        unload_start_minute=0,
    )
    dock.assigned_workers = 1
    dock.assigned_forklifts = 0
    dock.staging.occupancy_units = 0.0
    dock.staging.load_family = "floor"
    state.update_resource_assignment_counters()

    triggers = simulate_one_minute(state, config=config, rng=random.Random(77))

    assert any(event.trigger_type == "dock_freed" for event in triggers)
    assert len(state.completed_trucks) == 1
    completed = state.completed_trucks[0]
    assert completed.truck_id == "T-HIST-1"
    assert completed.departure_minute == state.now_minute
    assert completed.total_time_in_system_minutes == completed.departure_minute - completed.gate_arrival_minute

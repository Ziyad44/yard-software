from yard.models import DockState, ResourcePool, StagingAreaState, Truck


def _truck() -> Truck:
    return Truck(
        truck_id="T00001",
        truck_type="small_floor",
        initial_load_units=30.0,
        remaining_load_units=30.0,
        gate_arrival_minute=0,
    )


def test_dock_can_accept_only_when_truck_none_and_staging_empty() -> None:
    dock = DockState(
        dock_id=1,
        active=True,
        current_truck=None,
        staging=StagingAreaState(dock_id=1, occupancy_units=0.0, capacity_units=100.0),
    )
    assert dock.can_accept_next_truck()

    dock.current_truck = _truck()
    assert not dock.can_accept_next_truck()

    dock.current_truck = None
    dock.staging.occupancy_units = 2.0
    assert not dock.can_accept_next_truck()


def test_resource_pool_idle_counts() -> None:
    pool = ResourcePool(total_workers=8, total_forklifts=3, assigned_workers=5, assigned_forklifts=1)
    assert pool.idle_workers == 3
    assert pool.idle_forklifts == 2

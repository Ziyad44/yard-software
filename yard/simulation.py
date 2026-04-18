"""Core minute-step simulation and trigger detection helpers."""

from __future__ import annotations

import copy
import math
import random

from .config import YardConfig
from .models import EPSILON, DockState, LoadFamily, SystemSnapshot, TriggerEvent, Truck, YardState


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _poisson_sample(lam: float, rng: random.Random) -> int:
    """Sample Poisson arrivals with Knuth's algorithm (small lambda friendly)."""
    if lam <= 0.0:
        return 0
    l_value = math.exp(-lam)
    k = 0
    p = 1.0
    while p > l_value:
        k += 1
        p *= rng.random()
    return k - 1


def _sample_truck_type(mix: dict[str, float], rng: random.Random) -> str:
    roll = rng.random()
    cumulative = 0.0
    fallback = None
    for truck_type, probability in mix.items():
        fallback = truck_type
        cumulative += probability
        if roll <= cumulative:
            return truck_type
    if fallback is None:
        raise ValueError("truck type mix is empty")
    return fallback


def generate_arrivals_for_minute(state: YardState, config: YardConfig, rng: random.Random) -> list[Truck]:
    """Generate gate arrivals for one minute and append them to queue."""
    arrivals: list[Truck] = []
    lam = max(config.arrival_rate_per_hour, 0.0) * config.time_step_minutes / 60.0
    count = _poisson_sample(lam, rng)
    mix = config.normalized_truck_type_mix()

    for _ in range(count):
        truck_type = _sample_truck_type(mix, rng)
        load_units = float(config.truck_load_units[truck_type])
        truck = Truck(
            truck_id=f"T{state.next_truck_sequence:05d}",
            truck_type=truck_type,  # type: ignore[arg-type]
            initial_load_units=load_units,
            remaining_load_units=load_units,
            gate_arrival_minute=state.now_minute,
        )
        state.next_truck_sequence += 1
        arrivals.append(truck)

    state.waiting_queue.extend(arrivals)
    return arrivals


def _infer_load_family_for_staging_without_truck(dock: DockState) -> LoadFamily | None:
    """Best-effort fallback for seeded states that have staging load but no active truck object."""
    if dock.staging.occupancy_units <= EPSILON:
        return None
    if dock.assigned_workers > 0 and dock.assigned_forklifts <= 0:
        return "floor"
    if dock.assigned_forklifts > 0 and dock.assigned_workers <= 0:
        return "palletized"
    # If staging has load but metadata is missing, default to floor handling.
    # This keeps seeded/manual states operable while preserving exclusivity.
    return "floor"


def dock_load_family(dock: DockState) -> LoadFamily | None:
    """Return the active load family currently being handled at a dock, if known."""
    if dock.current_truck is not None:
        return dock.current_truck.load_family
    if dock.staging.load_family is not None and dock.staging.occupancy_units > EPSILON:
        return dock.staging.load_family
    return _infer_load_family_for_staging_without_truck(dock)


def sanitize_assignment_for_dock(
    *,
    dock: DockState,
    workers: int,
    forklifts: int,
    max_unloaders_per_dock: int,
) -> tuple[int, int]:
    """
    Enforce version-1 exclusivity:
    - floor load handling -> workers only
    - palletized load handling -> forklifts only
    """
    workers_nonneg = max(int(workers), 0)
    forklifts_nonneg = max(int(forklifts), 0)
    family = dock_load_family(dock)

    if family == "floor":
        return min(workers_nonneg, max(max_unloaders_per_dock, 0)), 0
    if family == "palletized":
        return 0, forklifts_nonneg
    return 0, 0


def compute_unload_rate(truck: Truck, dock: DockState, config: YardConfig) -> float:
    """Compute unload inflow capacity with load-type-exclusive resources."""
    if truck.is_floor_loaded:
        rate = config.floor_unload_worker_rate * max(dock.assigned_workers, 0)
    else:
        rate = config.pallet_unload_forklift_rate * max(dock.assigned_forklifts, 0)
    return max(rate, 0.0)


def compute_clear_rate(dock: DockState, config: YardConfig) -> float:
    """Compute staging outflow capacity using only the resource valid for the current load family."""
    family = dock_load_family(dock)
    if family == "floor":
        return max(config.clear_worker_rate * max(dock.assigned_workers, 0), 0.0)
    if family == "palletized":
        return max(config.clear_forklift_rate * max(dock.assigned_forklifts, 0), 0.0)
    return 0.0


def update_busy_dock_one_step(dock: DockState, config: YardConfig) -> bool:
    """
    Apply one simulation step for a dock.

    Returns:
      True if the dock became free this minute (strict release condition).
    """
    # Inactive docks still need to finish any in-progress unloading/clearing work.
    # `active` controls future dispatch eligibility, not whether existing load can move.
    if not dock.active and dock.current_truck is None and dock.staging.occupancy_units <= EPSILON:
        return False

    dt = float(config.time_step_minutes)
    clear_rate = compute_clear_rate(dock, config)

    if dock.current_truck is None:
        if dock.staging.occupancy_units > EPSILON:
            dock.staging.occupancy_units = max(dock.staging.occupancy_units - clear_rate * dt, 0.0)
            if dock.staging.occupancy_units <= EPSILON:
                dock.staging.occupancy_units = 0.0
                dock.staging.load_family = None
        return False

    truck = dock.current_truck
    dock.staging.load_family = truck.load_family
    unload_rate = compute_unload_rate(truck, dock, config)
    headroom = max(dock.staging.capacity_units - dock.staging.occupancy_units, 0.0)
    inflow = min(unload_rate * dt, truck.remaining_load_units, headroom)
    outflow = min(clear_rate * dt, dock.staging.occupancy_units + inflow)

    dock.staging.occupancy_units = _clamp(
        dock.staging.occupancy_units + inflow - outflow,
        0.0,
        dock.staging.capacity_units,
    )
    truck.remaining_load_units = max(truck.remaining_load_units - inflow, 0.0)

    if truck.remaining_load_units <= EPSILON and dock.staging.occupancy_units <= EPSILON:
        truck.remaining_load_units = 0.0
        dock.staging.occupancy_units = 0.0
        dock.staging.load_family = None
        dock.current_truck = None
        return True

    return False


def _allocate_minimum_resources_for_new_truck(
    state: YardState,
    dock: DockState,
    config: YardConfig | None,
) -> bool:
    """
    Ensure a newly assigned busy dock can make forward progress.

    Version-1 rule:
    idle docks carry zero assignments, so when they receive a truck we
    allocate the single resource type required by the truck load family
    from currently idle global resources.
    """
    truck = dock.current_truck
    if truck is None:
        return False
    if config is None:
        return False

    before_workers = dock.assigned_workers
    before_forklifts = dock.assigned_forklifts
    max_workers = max(config.max_unloaders_per_dock, 0)

    if truck.is_floor_loaded:
        dock.assigned_forklifts = 0
        if compute_unload_rate(truck, dock, config) > EPSILON:
            return (
                dock.assigned_workers != before_workers
                or dock.assigned_forklifts != before_forklifts
            )
        if state.resources.idle_workers <= 0 or dock.assigned_workers >= max_workers:
            return (
                dock.assigned_workers != before_workers
                or dock.assigned_forklifts != before_forklifts
            )
        dock.assigned_workers += 1
        return True

    dock.assigned_workers = 0
    if compute_unload_rate(truck, dock, config) > EPSILON:
        return (
            dock.assigned_workers != before_workers
            or dock.assigned_forklifts != before_forklifts
        )
    if state.resources.idle_forklifts <= 0:
        return (
            dock.assigned_workers != before_workers
            or dock.assigned_forklifts != before_forklifts
        )
    dock.assigned_forklifts += 1
    return True


def dispatch_waiting_trucks(
    state: YardState,
    minute: int,
    config: YardConfig | None = None,
) -> None:
    """Assign queued trucks to docks that are fully free, unless gate hold is active."""
    if state.hold_gate_release:
        return

    assignments_changed = False
    for dock_id in sorted(state.docks):
        if not state.waiting_queue:
            break
        dock = state.docks[dock_id]
        if not dock.can_accept_next_truck():
            continue
        truck = state.waiting_queue.pop(0)
        truck.assigned_dock_id = dock_id
        truck.unload_start_minute = minute
        dock.current_truck = truck
        dock.staging.load_family = truck.load_family
        if _allocate_minimum_resources_for_new_truck(state, dock, config):
            assignments_changed = True
            state.update_resource_assignment_counters()

    if assignments_changed:
        state.update_resource_assignment_counters()


def detect_staging_threshold_event(dock: DockState, minute: int) -> TriggerEvent | None:
    """Emit threshold trigger on low->high crossing, reset only below low threshold."""
    ratio = dock.staging.occupancy_ratio
    if ratio >= dock.staging.threshold_high and not dock.staging.threshold_alert_active:
        dock.staging.threshold_alert_active = True
        return TriggerEvent(
            trigger_type="staging_threshold",
            minute=minute,
            dock_id=dock.dock_id,
            reason=f"Dock {dock.dock_id} staging crossed high threshold ({ratio:.2f}).",
        )
    if ratio <= dock.staging.threshold_low and dock.staging.threshold_alert_active:
        dock.staging.threshold_alert_active = False
    return None


def detect_review_timer_event(state: YardState, minute: int, config: YardConfig) -> TriggerEvent | None:
    """Emit timer trigger when review cadence is reached."""
    if minute < state.next_review_minute:
        return None
    if config.review_interval_minutes <= 0:
        raise ValueError("review_interval_minutes must be > 0")
    while state.next_review_minute <= minute:
        state.next_review_minute += config.review_interval_minutes
    state.last_review_minute = minute
    return TriggerEvent(
        trigger_type="review_timer",
        minute=minute,
        dock_id=None,
        reason="Review interval reached.",
    )


def simulate_one_minute(state: YardState, config: YardConfig, rng: random.Random) -> list[TriggerEvent]:
    """Advance the live state by one minute and return trigger events."""
    minute = state.now_minute + config.time_step_minutes
    state.now_minute = minute

    generate_arrivals_for_minute(state, config, rng)
    triggers: list[TriggerEvent] = []

    for dock in state.docks.values():
        was_freed = update_busy_dock_one_step(dock, config)
        if was_freed:
            triggers.append(
                TriggerEvent(
                    trigger_type="dock_freed",
                    minute=minute,
                    dock_id=dock.dock_id,
                    reason=f"Dock {dock.dock_id} is now free.",
                )
            )

    dispatch_waiting_trucks(state, minute=minute, config=config)

    for dock in state.docks.values():
        threshold_event = detect_staging_threshold_event(dock, minute=minute)
        if threshold_event is not None:
            triggers.append(threshold_event)

    timer_event = detect_review_timer_event(state, minute=minute, config=config)
    if timer_event is not None:
        triggers.append(timer_event)

    state.recent_triggers = triggers
    return triggers


def simulate_horizon(
    state: YardState,
    config: YardConfig,
    minutes: int,
    rng: random.Random,
) -> SystemSnapshot:
    """
    Simulate a short lookahead horizon from a copied state.

    Version-1 note:
    This remains intentionally lightweight for Phase 1 and is refined in Phase 2.
    """
    if minutes <= 0:
        raise ValueError("minutes must be > 0")

    temp_state = copy.deepcopy(state)
    queue_sum = 0.0
    utilization_sum = 0.0
    staging_risk_sum = 0.0

    for _ in range(minutes):
        simulate_one_minute(temp_state, config, rng)
        queue_sum += temp_state.queue_length

        active_docks = [d for d in temp_state.docks.values() if d.active]
        busy_docks = [d for d in active_docks if d.current_truck is not None]
        if active_docks:
            utilization_sum += len(busy_docks) / len(active_docks)
            staging_risk_sum += sum(
                1.0
                for d in active_docks
                if d.staging.occupancy_ratio >= d.staging.threshold_high
            ) / len(active_docks)

    avg_queue = queue_sum / minutes
    avg_utilization = utilization_sum / minutes if minutes > 0 else 0.0
    avg_staging_risk = staging_risk_sum / minutes if minutes > 0 else 0.0

    # Placeholder for Phase 2. Average waiting is approximated by average queue in this scaffold.
    approx_avg_wait = avg_queue
    approx_avg_tis = approx_avg_wait + 20.0

    return SystemSnapshot(
        minute=temp_state.now_minute,
        queue_length=temp_state.queue_length,
        predicted_avg_wait_minutes=approx_avg_wait,
        predicted_avg_time_in_system_minutes=approx_avg_tis,
        predicted_dock_utilization=avg_utilization,
        predicted_staging_overflow_risk=avg_staging_risk,
        recommended_action_text=None,
        docks=[],
        resource_summary={},
        verification_placeholder={},
    )

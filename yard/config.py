"""Configuration defaults for the version-1 smart yard model."""

from __future__ import annotations

from dataclasses import dataclass, field


TRUCK_TYPES: tuple[str, ...] = (
    "small_floor",
    "medium_floor",
    "large_floor",
    "small_palletized",
    "medium_palletized",
    "large_palletized",
)


DEFAULT_TRUCK_TYPE_MIX: dict[str, float] = {
    "small_floor": 0.24,
    "medium_floor": 0.35,
    "large_floor": 0.12,
    "small_palletized": 0.06,
    "medium_palletized": 0.15,
    "large_palletized": 0.08,
}


DEFAULT_TRUCK_LOAD_UNITS: dict[str, float] = {
    "small_floor": 30.0,
    "medium_floor": 50.0,
    "large_floor": 70.0,
    "small_palletized": 24.0,
    "medium_palletized": 40.0,
    "large_palletized": 56.0,
}


@dataclass(frozen=True)
class YardConfig:
    """Tunable constants for the version-1 simulation and recommendation loop."""

    time_step_minutes: int = 1
    review_interval_minutes: int = 15
    lookahead_horizon_minutes: int = 30

    staging_capacity_units: float = 100.0
    staging_high_threshold: float = 0.85
    staging_low_threshold: float = 0.75

    max_unloaders_per_dock: int = 4
    arrival_rate_per_hour: float = 8.0

    # Rate coefficients (units / minute)
    floor_unload_worker_rate: float = 1.4
    floor_unload_forklift_assist_rate: float = 0.2
    pallet_unload_forklift_rate: float = 2.2
    pallet_unload_worker_assist_rate: float = 0.2
    clear_worker_rate: float = 0.8
    clear_forklift_rate: float = 1.2

    # Avoid unnecessary action churn
    min_score_improvement_to_switch: float = 0.05

    truck_type_mix: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TRUCK_TYPE_MIX))
    truck_load_units: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TRUCK_LOAD_UNITS))

    def normalized_truck_type_mix(self) -> dict[str, float]:
        """Return a normalized truck mix over known truck types."""
        filtered = {k: float(v) for k, v in self.truck_type_mix.items() if k in TRUCK_TYPES and v > 0.0}
        total = sum(filtered.values())
        if total <= 0.0:
            return dict(DEFAULT_TRUCK_TYPE_MIX)
        return {k: v / total for k, v in filtered.items()}

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
    "small_floor": 6.0,
    "medium_floor": 12.0,
    "large_floor": 30.0,
    "small_palletized": 6.0,
    "medium_palletized": 12.0,
    "large_palletized": 30.0,
}

DEFAULT_STAGING_CAPACITY_UNITS: float = 40.0


@dataclass(frozen=True)
class YardConfig:
    """Tunable constants for the version-1 simulation and recommendation loop."""

    time_step_minutes: int = 1
    review_interval_minutes: int = 15
    lookahead_horizon_minutes: int = 30
    forecast_window_minutes: int = 30
    forecast_smoothing_alpha: float = 0.35
    forecast_scenario_delta: float = 0.20

    staging_capacity_units: float = DEFAULT_STAGING_CAPACITY_UNITS
    staging_high_threshold: float = 0.85
    staging_low_threshold: float = 0.75

    max_unloaders_per_dock: int = 4
    arrival_rate_per_hour: float = 8.0

    # Rate coefficients (units / minute)
    # Handling is load-type-exclusive:
    # - floor-loaded trucks: workers only
    # - palletized trucks: forklifts only
    floor_unload_worker_rate: float = 1.4
    pallet_unload_forklift_rate: float = 2.2
    # Clearing is also load-type-exclusive:
    # - floor load in staging clears via workers
    # - palletized load in staging clears via forklifts
    clear_worker_rate: float = 0.8
    clear_forklift_rate: float = 1.2

    # Avoid unnecessary action churn
    min_score_improvement_to_switch: float = 0.05
    evaluation_replications: int = 8
    verification_littles_law_threshold: float = 0.10
    verification_ci_ratio_threshold: float = 0.20

    truck_type_mix: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TRUCK_TYPE_MIX))
    truck_load_units: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TRUCK_LOAD_UNITS))

    def normalized_truck_type_mix(self) -> dict[str, float]:
        """Return a normalized truck mix over known truck types."""
        filtered = {k: float(v) for k, v in self.truck_type_mix.items() if k in TRUCK_TYPES and v > 0.0}
        total = sum(filtered.values())
        if total <= 0.0:
            return dict(DEFAULT_TRUCK_TYPE_MIX)
        return {k: v / total for k, v in filtered.items()}

    def resolved_truck_load_units(self) -> dict[str, float]:
        """
        Return a complete load mapping over known truck types.

        Missing, zero, or negative values fall back to defaults so runtime
        sampling and service-time estimates stay valid.
        """
        source = self.truck_load_units if isinstance(self.truck_load_units, dict) else {}
        resolved: dict[str, float] = {}
        for truck_type in TRUCK_TYPES:
            fallback = float(DEFAULT_TRUCK_LOAD_UNITS[truck_type])
            candidate = source.get(truck_type, fallback)
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                value = fallback
            resolved[truck_type] = value if value > 0.0 else fallback
        return resolved

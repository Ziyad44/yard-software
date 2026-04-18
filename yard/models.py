"""State dataclasses for the version-1 smart yard backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


EPSILON = 1e-6

TruckType = Literal[
    "small_floor",
    "medium_floor",
    "large_floor",
    "small_palletized",
    "medium_palletized",
    "large_palletized",
]
TriggerType = Literal["dock_freed", "staging_threshold", "review_timer"]
LoadFamily = Literal["floor", "palletized"]


@dataclass
class Truck:
    """Truck state tracked from gate arrival through dock completion."""

    truck_id: str
    truck_type: TruckType
    initial_load_units: float
    remaining_load_units: float
    gate_arrival_minute: int
    assigned_dock_id: Optional[int] = None
    unload_start_minute: Optional[int] = None
    departure_minute: Optional[int] = None

    def __post_init__(self) -> None:
        if self.initial_load_units < 0.0:
            raise ValueError("initial_load_units must be non-negative")
        if self.remaining_load_units < 0.0:
            raise ValueError("remaining_load_units must be non-negative")
        if self.departure_minute is not None and self.departure_minute < self.gate_arrival_minute:
            raise ValueError("departure_minute cannot be earlier than gate_arrival_minute")

    @property
    def is_floor_loaded(self) -> bool:
        return self.truck_type.endswith("_floor")

    @property
    def load_family(self) -> LoadFamily:
        return "floor" if self.is_floor_loaded else "palletized"

    @property
    def total_time_in_system_minutes(self) -> Optional[int]:
        if self.departure_minute is None:
            return None
        return max(self.departure_minute - self.gate_arrival_minute, 0)

    @property
    def waiting_time_before_unload_minutes(self) -> Optional[int]:
        if self.unload_start_minute is None:
            return None
        return max(self.unload_start_minute - self.gate_arrival_minute, 0)

    @property
    def service_time_minutes(self) -> Optional[int]:
        if self.departure_minute is None or self.unload_start_minute is None:
            return None
        return max(self.departure_minute - self.unload_start_minute, 0)


@dataclass
class StagingAreaState:
    """One staging area per dock with fixed version-1 capacity."""

    dock_id: int
    occupancy_units: float = 0.0
    capacity_units: float = 100.0
    threshold_high: float = 0.85
    threshold_low: float = 0.75
    threshold_alert_active: bool = False
    load_family: Optional[LoadFamily] = None

    @property
    def occupancy_ratio(self) -> float:
        if self.capacity_units <= 0:
            return 0.0
        return self.occupancy_units / self.capacity_units


@dataclass
class DockState:
    """Dock state with current truck, assignments, and staging."""

    dock_id: int
    active: bool = True
    current_truck: Optional[Truck] = None
    assigned_workers: int = 0
    assigned_forklifts: int = 0
    staging: StagingAreaState = field(default_factory=lambda: StagingAreaState(dock_id=0))

    @property
    def phase(self) -> str:
        if self.current_truck is None:
            return "idle" if self.staging.occupancy_units <= EPSILON else "clearing"
        if self.current_truck.remaining_load_units > EPSILON:
            return "unloading"
        return "clearing"

    def can_accept_next_truck(self) -> bool:
        """Strict release condition: no truck and empty staging."""
        return self.active and self.current_truck is None and self.staging.occupancy_units <= EPSILON


@dataclass
class ResourcePool:
    """Global resources supplied by the supervisor."""

    total_workers: int
    total_forklifts: int
    assigned_workers: int = 0
    assigned_forklifts: int = 0

    @property
    def idle_workers(self) -> int:
        return max(self.total_workers - self.assigned_workers, 0)

    @property
    def idle_forklifts(self) -> int:
        return max(self.total_forklifts - self.assigned_forklifts, 0)


@dataclass
class TriggerEvent:
    """Discrete trigger used to gate recommendation runs."""

    trigger_type: TriggerType
    minute: int
    dock_id: Optional[int]
    reason: str


@dataclass
class Action:
    """Supervisor-facing action payload."""

    action_name: str
    workers_by_dock: dict[int, int]
    forklifts_by_dock: dict[int, int]
    hold_gate_release: bool = False
    notes: str = ""


@dataclass
class ForecastResult:
    """ISE-style short-horizon arrival forecast summary."""

    baseline_rate_per_hour: float
    smoothed_rate_per_hour: float
    expected_arrivals: float
    scenarios: dict[str, float] = field(default_factory=dict)
    window_minutes: int = 0
    observed_arrivals: int = 0


@dataclass
class ScenarioMetrics:
    """Scenario-level KPI bundle used for robust action comparison."""

    scenario_name: str
    arrival_rate_per_hour: float
    predicted_avg_wait_minutes: float
    predicted_avg_time_in_system_minutes: float
    predicted_queue_length: float
    predicted_avg_number_in_system: float
    predicted_dock_utilization: float
    predicted_staging_overflow_risk: float
    throughput_trucks_per_hour: float
    effective_flow_rate_per_hour: float
    score: float


@dataclass
class ActionEvaluation:
    """Near-term simulated outcome for one candidate action."""

    action: Action
    predicted_avg_wait_minutes: float
    predicted_avg_time_in_system_minutes: float
    predicted_queue_length: float
    predicted_dock_utilization: float
    predicted_staging_overflow_risk: float
    score: float
    predicted_avg_number_in_system: float = 0.0
    throughput_trucks_per_hour: float = 0.0
    effective_flow_rate_per_hour: float = 0.0
    robust_score: float = 0.0
    scenario_metrics: dict[str, ScenarioMetrics] = field(default_factory=dict)
    replication_count: int = 1
    replication_avg_tis: list[float] = field(default_factory=list)
    verification: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Recommendation:
    """Selected action plus explanation and candidate comparison."""

    selected_action: Action
    rationale: str
    score: float
    candidate_scores: dict[str, float] = field(default_factory=dict)
    robust_score: float = 0.0
    forecast: Optional[ForecastResult] = None
    evaluations: list[ActionEvaluation] = field(default_factory=list)
    verification: dict[str, dict[str, Any]] = field(default_factory=dict)
    selected_baseline_metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class DockSummary:
    """Dashboard-friendly per-dock summary snapshot."""

    dock_id: int
    phase: str
    current_truck_id: Optional[str]
    current_truck_type: Optional[str]
    remaining_load_units: float
    staging_occupancy_units: float
    staging_capacity_units: float
    assigned_workers: int
    assigned_forklifts: int


@dataclass
class SystemSnapshot:
    """Dashboard-friendly system snapshot."""

    minute: int
    queue_length: int
    predicted_avg_wait_minutes: Optional[float]
    predicted_avg_time_in_system_minutes: Optional[float]
    predicted_dock_utilization: Optional[float]
    predicted_staging_overflow_risk: Optional[float]
    recommended_action_text: Optional[str]
    predicted_queue_length: Optional[float] = None
    predicted_avg_number_in_system: Optional[float] = None
    predicted_throughput_trucks_per_hour: Optional[float] = None
    predicted_effective_flow_rate_per_hour: Optional[float] = None
    forecast_summary: dict[str, Any] = field(default_factory=dict)
    verification_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    ise_evaluations: list[dict[str, Any]] = field(default_factory=list)
    docks: list[DockSummary] = field(default_factory=list)
    resource_summary: dict[str, int] = field(default_factory=dict)
    verification_placeholder: dict[str, str] = field(default_factory=dict)


@dataclass
class YardState:
    """Single source of truth for live system state."""

    now_minute: int
    waiting_queue: list[Truck] = field(default_factory=list)
    completed_trucks: list[Truck] = field(default_factory=list)
    docks: dict[int, DockState] = field(default_factory=dict)
    resources: ResourcePool = field(default_factory=lambda: ResourcePool(total_workers=0, total_forklifts=0))
    active_action: Optional[Action] = None
    last_recommendation: Optional[Recommendation] = None
    hold_gate_release: bool = False

    last_review_minute: int = 0
    next_review_minute: int = 15
    recent_triggers: list[TriggerEvent] = field(default_factory=list)
    arrival_history: list[tuple[int, int]] = field(default_factory=list)
    kpi_cache: dict[str, float] = field(default_factory=dict)
    next_truck_sequence: int = 1
    recent_replication_means: list[float] = field(default_factory=list)
    last_ise_output: dict[str, Any] = field(default_factory=dict)

    @property
    def queue_length(self) -> int:
        return len(self.waiting_queue)

    def update_resource_assignment_counters(self) -> None:
        self.resources.assigned_workers = sum(max(d.assigned_workers, 0) for d in self.docks.values() if d.active)
        self.resources.assigned_forklifts = sum(max(d.assigned_forklifts, 0) for d in self.docks.values() if d.active)

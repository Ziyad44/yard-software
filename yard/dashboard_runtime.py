"""Dashboard runtime state and UI-facing payload contract."""

from __future__ import annotations

import copy
import dataclasses
import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Any

from .config import YardConfig
from .engine import (
    apply_action,
    initialize_state,
    refresh_kpi_cache,
    run_minute_cycle,
    snapshot_from_state,
    update_supervisor_inputs,
)
from .models import Action, DockState, Recommendation, TriggerEvent, YardState
from .simulation import compute_clear_rate, update_busy_dock_one_step


MAX_ETA_MINUTES = 12 * 60
DEFAULT_HISTORY_WINDOW = 120


@dataclass
class TrendPoint:
    minute: int
    queue_length: int
    arrivals: int
    max_staging_occupancy_pct: float


@dataclass
class DashboardRuntime:
    """Holds live backend state and presents a dashboard-friendly contract."""

    config: YardConfig
    state: YardState
    rng_seed: int = 42
    history_window_minutes: int = DEFAULT_HISTORY_WINDOW
    rng: random.Random = field(init=False)
    trend_history: list[TrendPoint] = field(default_factory=list)
    last_trigger_batch: list[TriggerEvent] = field(default_factory=list)
    recommendation_trigger_batch: list[TriggerEvent] = field(default_factory=list)
    last_recommendation_minute: int | None = None
    recommendation_applied: bool = False
    recommendation_decision: str = "none"

    def __post_init__(self) -> None:
        self.rng = random.Random(self.rng_seed)
        if self.state.active_action is None:
            self.apply_balanced_starting_action()
        self._enforce_idle_dock_zero_assignments()
        refresh_kpi_cache(self.state, self.config)
        self._record_trend(arrivals=0)

    @classmethod
    def create_default(cls) -> "DashboardRuntime":
        config = YardConfig(
            arrival_rate_per_hour=20.0,
            review_interval_minutes=5,
            lookahead_horizon_minutes=20,
        )
        state = initialize_state(
            available_workers=8,
            available_forklifts=3,
            active_docks=4,
            max_unloaders_per_dock=config.max_unloaders_per_dock,
            config=config,
        )
        runtime = cls(config=config, state=state)
        return runtime

    def apply_balanced_starting_action(self) -> None:
        active_dock_ids = sorted(dock_id for dock_id, dock in self.state.docks.items() if dock.active)
        if not active_dock_ids:
            return

        busy_dock_ids = [
            dock_id
            for dock_id in active_dock_ids
            if self.state.docks[dock_id].current_truck is not None
            or self.state.docks[dock_id].staging.occupancy_units > 0.0
        ]
        target_docks = busy_dock_ids if busy_dock_ids else []
        workers_by_dock = {dock_id: 0 for dock_id in active_dock_ids}
        forklifts_by_dock = {dock_id: 0 for dock_id in active_dock_ids}
        dock_count = len(target_docks)

        if dock_count > 0:
            for idx in range(self.state.resources.total_workers):
                dock_id = target_docks[idx % dock_count]
                if workers_by_dock[dock_id] < self.config.max_unloaders_per_dock:
                    workers_by_dock[dock_id] += 1
            for idx in range(self.state.resources.total_forklifts):
                dock_id = target_docks[idx % dock_count]
                forklifts_by_dock[dock_id] += 1

        apply_action(
            self.state,
            Action(
                action_name="initial_busy_dock_assignment",
                workers_by_dock=workers_by_dock,
                forklifts_by_dock=forklifts_by_dock,
                hold_gate_release=False,
                notes="Startup assignment only for currently busy docks.",
            ),
            config=self.config,
        )

    def step(self, minutes: int = 1) -> dict[str, Any]:
        if minutes <= 0:
            raise ValueError("minutes must be positive")

        for _ in range(minutes):
            seq_before = self.state.next_truck_sequence
            triggers, recommendation = run_minute_cycle(self.state, self.config, rng=self.rng)
            self._enforce_idle_dock_zero_assignments()
            arrivals = max(self.state.next_truck_sequence - seq_before, 0)
            self._record_trend(arrivals=arrivals)

            self.last_trigger_batch = triggers
            if recommendation is not None:
                self.last_recommendation_minute = self.state.now_minute
                self.recommendation_trigger_batch = list(triggers)
                self.recommendation_applied = False
                self.recommendation_decision = "pending"

        return self.get_dashboard_payload()

    def apply_recommendation(self) -> dict[str, Any]:
        recommendation = self.state.last_recommendation
        if recommendation is None:
            raise ValueError("No recommendation available to apply.")
        apply_action(self.state, recommendation.selected_action, config=self.config)
        self._enforce_idle_dock_zero_assignments()
        refresh_kpi_cache(self.state, self.config)
        self.recommendation_applied = True
        self.recommendation_decision = "applied"
        return self.get_dashboard_payload()

    def keep_current_plan(self) -> dict[str, Any]:
        if self.state.last_recommendation is None:
            raise ValueError("No recommendation available to keep.")
        workers_by_dock = {
            dock_id: max(dock.assigned_workers, 0)
            for dock_id, dock in self.state.docks.items()
            if dock.active
        }
        forklifts_by_dock = {
            dock_id: max(dock.assigned_forklifts, 0)
            for dock_id, dock in self.state.docks.items()
            if dock.active
        }
        action = Action(
            action_name="keep_current_plan",
            workers_by_dock=workers_by_dock,
            forklifts_by_dock=forklifts_by_dock,
            hold_gate_release=self.state.hold_gate_release,
            notes="Supervisor chose to keep current plan.",
        )
        apply_action(self.state, action, config=self.config)
        self._enforce_idle_dock_zero_assignments()
        refresh_kpi_cache(self.state, self.config)
        self.recommendation_applied = False
        self.recommendation_decision = "kept_current_plan"
        return self.get_dashboard_payload()

    def update_supervisor(self, payload: dict[str, Any]) -> dict[str, Any]:
        available_workers = self._coerce_int(payload.get("available_workers"), minimum=0, fallback=None)
        available_forklifts = self._coerce_int(payload.get("available_forklifts"), minimum=0, fallback=None)
        active_docks = self._coerce_int(payload.get("active_docks"), minimum=0, fallback=None)
        max_unloaders = self._coerce_int(payload.get("max_unloaders_per_dock"), minimum=1, fallback=None)

        update_supervisor_inputs(
            self.state,
            available_workers=available_workers,
            available_forklifts=available_forklifts,
            active_docks=active_docks,
        )
        self._sync_new_dock_defaults()

        if max_unloaders is not None and max_unloaders != self.config.max_unloaders_per_dock:
            self.config = dataclasses.replace(self.config, max_unloaders_per_dock=max_unloaders)
        self._rebalance_assignments_to_constraints()
        self._enforce_idle_dock_zero_assignments()
        refresh_kpi_cache(self.state, self.config)
        return self.get_dashboard_payload()

    def get_dashboard_payload(self) -> dict[str, Any]:
        snapshot = snapshot_from_state(self.state)
        staging_cards = []
        dock_rows = []

        for dock_summary in snapshot.docks:
            occupancy_pct = 0.0
            if dock_summary.staging_capacity_units > 0.0:
                occupancy_pct = 100.0 * dock_summary.staging_occupancy_units / dock_summary.staging_capacity_units
            traffic_light = self._traffic_light(occupancy_pct)
            dock = self.state.docks[dock_summary.dock_id]
            eta_minutes = self._estimate_eta_minutes(dock)
            eta_text = "N/A" if eta_minutes is None else f"{eta_minutes} min"
            status = "busy" if dock_summary.phase != "idle" else "idle"

            staging_cards.append(
                {
                    "dock_id": dock_summary.dock_id,
                    "occupancy_pct": round(occupancy_pct, 1),
                    "occupancy_units": round(dock_summary.staging_occupancy_units, 1),
                    "capacity_units": round(dock_summary.staging_capacity_units, 1),
                    "traffic_light": traffic_light,
                }
            )
            dock_rows.append(
                {
                    "dock_id": dock_summary.dock_id,
                    "status": status,
                    "phase": dock_summary.phase,
                    "truck_id": dock_summary.current_truck_id,
                    "truck_type": dock_summary.current_truck_type,
                    "assigned_workers": dock_summary.assigned_workers,
                    "assigned_forklifts": dock_summary.assigned_forklifts,
                    "staging_occupancy_units": round(dock_summary.staging_occupancy_units, 1),
                    "staging_occupancy_pct": round(occupancy_pct, 1),
                    "eta_text": eta_text,
                }
            )

        recommendation_obj = self._serialize_recommendation(self.state.last_recommendation)
        trigger_source_events = (
            self.recommendation_trigger_batch
            if self.state.last_recommendation is not None
            else []
        )
        trigger_source = [
            f"{event.trigger_type} (dock {event.dock_id})" if event.dock_id else event.trigger_type
            for event in trigger_source_events
        ]
        trend_points = self.trend_history[-self.history_window_minutes :]

        decision_status = self.recommendation_decision
        is_applied = self.recommendation_applied
        if self.state.last_recommendation is None:
            decision_status = "none"
            is_applied = False

        return {
            "minute": self.state.now_minute,
            "supervisor_inputs": {
                "available_workers": self.state.resources.total_workers,
                "available_forklifts": self.state.resources.total_forklifts,
                "active_docks": sum(1 for dock in self.state.docks.values() if dock.active),
                "max_unloaders_per_dock": self.config.max_unloaders_per_dock,
            },
            "kpis": {
                "queue_length": snapshot.queue_length,
                "predicted_avg_wait_minutes": round(float(snapshot.predicted_avg_wait_minutes or 0.0), 2),
                "predicted_avg_time_in_system_minutes": round(
                    float(snapshot.predicted_avg_time_in_system_minutes or 0.0), 2
                ),
                "dock_utilization": round(100.0 * float(snapshot.predicted_dock_utilization or 0.0), 1),
                "staging_risk_pct": round(100.0 * float(snapshot.predicted_staging_overflow_risk or 0.0), 1),
                "recommended_action": recommendation_obj["text"],
            },
            "recommendation": {
                **recommendation_obj,
                "trigger_source": trigger_source,
                "is_applied": is_applied,
                "decision_status": decision_status,
                "minute_generated": self.last_recommendation_minute,
            },
            "staging_status": staging_cards,
            "dock_status": dock_rows,
            "resource_summary": snapshot.resource_summary,
            "verification": self._build_verification_cards(snapshot=snapshot, trend_points=trend_points),
            "trends": {
                "minutes": [point.minute for point in trend_points],
                "queue_length": [point.queue_length for point in trend_points],
                "arrivals": [point.arrivals for point in trend_points],
                "max_staging_occupancy_pct": [point.max_staging_occupancy_pct for point in trend_points],
            },
        }

    def _record_trend(self, arrivals: int) -> None:
        active_docks = [dock for dock in self.state.docks.values() if dock.active]
        max_staging_pct = 0.0
        if active_docks:
            max_staging_pct = max(100.0 * dock.staging.occupancy_ratio for dock in active_docks)

        self.trend_history.append(
            TrendPoint(
                minute=self.state.now_minute,
                queue_length=self.state.queue_length,
                arrivals=arrivals,
                max_staging_occupancy_pct=round(max_staging_pct, 1),
            )
        )
        if len(self.trend_history) > self.history_window_minutes * 2:
            self.trend_history = self.trend_history[-self.history_window_minutes :]

    def _sync_new_dock_defaults(self) -> None:
        for dock in self.state.docks.values():
            if dock.staging.capacity_units <= 0.0:
                dock.staging.capacity_units = self.config.staging_capacity_units
            dock.staging.threshold_high = self.config.staging_high_threshold
            dock.staging.threshold_low = self.config.staging_low_threshold
            if dock.staging.occupancy_units > dock.staging.capacity_units:
                dock.staging.occupancy_units = dock.staging.capacity_units
            if not dock.active:
                dock.assigned_workers = 0
                dock.assigned_forklifts = 0
        self.state.update_resource_assignment_counters()

    def _rebalance_assignments_to_constraints(self) -> None:
        for dock in self.state.docks.values():
            if not dock.active:
                dock.assigned_workers = 0
                dock.assigned_forklifts = 0
                continue
            dock.assigned_workers = min(max(dock.assigned_workers, 0), self.config.max_unloaders_per_dock)
            dock.assigned_forklifts = max(dock.assigned_forklifts, 0)

        self._trim_assignment("workers", self.state.resources.total_workers)
        self._trim_assignment("forklifts", self.state.resources.total_forklifts)
        self.state.update_resource_assignment_counters()

    def _enforce_idle_dock_zero_assignments(self) -> None:
        """
        Version-1 dashboard rule:
        idle docks must not retain worker/forklift assignments.
        """
        changed = False
        for dock in self.state.docks.values():
            if not dock.active or dock.phase == "idle":
                if dock.assigned_workers != 0:
                    dock.assigned_workers = 0
                    changed = True
                if dock.assigned_forklifts != 0:
                    dock.assigned_forklifts = 0
                    changed = True
        if changed:
            self.state.update_resource_assignment_counters()
            if self.state.active_action is not None:
                self.state.active_action = Action(
                    action_name=self.state.active_action.action_name,
                    workers_by_dock={
                        dock_id: max(dock.assigned_workers, 0)
                        for dock_id, dock in self.state.docks.items()
                        if dock.active
                    },
                    forklifts_by_dock={
                        dock_id: max(dock.assigned_forklifts, 0)
                        for dock_id, dock in self.state.docks.items()
                        if dock.active
                    },
                    hold_gate_release=self.state.hold_gate_release,
                    notes=self.state.active_action.notes,
                )

    def _trim_assignment(self, resource: str, total_limit: int) -> None:
        if resource == "workers":
            getter = lambda dock: dock.assigned_workers
            setter = lambda dock, value: setattr(dock, "assigned_workers", value)
        else:
            getter = lambda dock: dock.assigned_forklifts
            setter = lambda dock, value: setattr(dock, "assigned_forklifts", value)

        active_docks = [dock for dock in self.state.docks.values() if dock.active]
        assigned = sum(getter(dock) for dock in active_docks)
        if assigned <= total_limit:
            return
        overflow = assigned - total_limit
        for dock in sorted(active_docks, key=getter, reverse=True):
            if overflow <= 0:
                break
            current = getter(dock)
            if current <= 0:
                continue
            reduction = min(current, overflow)
            setter(dock, current - reduction)
            overflow -= reduction

    def _estimate_eta_minutes(self, dock: DockState) -> int | None:
        if dock.current_truck is None and dock.staging.occupancy_units <= 0.0:
            return 0
        if dock.current_truck is None:
            clear_rate = compute_clear_rate(dock, self.config)
            if clear_rate <= 0.0:
                return None
            return int(math.ceil(dock.staging.occupancy_units / clear_rate))

        dock_copy = copy.deepcopy(dock)
        for minute in range(1, MAX_ETA_MINUTES + 1):
            if update_busy_dock_one_step(dock_copy, self.config):
                return minute
        return None

    def _build_verification_cards(
        self,
        *,
        snapshot: Any,
        trend_points: list[TrendPoint],
    ) -> dict[str, dict[str, Any]]:
        spec3 = self._compute_spec3_card(snapshot=snapshot, trend_points=trend_points)
        spec4 = self._compute_spec4_card(trend_points=trend_points)
        return {"spec_3": spec3, "spec_4": spec4}

    def _compute_spec3_card(
        self,
        *,
        snapshot: Any,
        trend_points: list[TrendPoint],
    ) -> dict[str, Any]:
        title = "Spec 3 - Little's Law"
        if len(trend_points) < 6:
            return {
                "title": title,
                "status": "insufficient_data",
                "current_value": "Need at least 6 minutes of trend data.",
                "target": "relative error <= 25%",
                "value": None,
                "threshold": 0.25,
            }

        avg_queue = statistics.mean(point.queue_length for point in trend_points)
        total_arrivals = sum(point.arrivals for point in trend_points[1:])
        observed_minutes = max(len(trend_points) - 1, 1)
        lambda_per_min = total_arrivals / observed_minutes
        avg_wait = float(snapshot.predicted_avg_wait_minutes or 0.0)
        lambda_times_w = lambda_per_min * avg_wait
        baseline = max(avg_queue, lambda_times_w, 1e-6)
        relative_error = abs(avg_queue - lambda_times_w) / baseline
        status = "pass" if relative_error <= 0.25 else "warn"

        return {
            "title": title,
            "status": status,
            "current_value": (
                f"L={avg_queue:.2f}, lambda*W={lambda_times_w:.2f}, "
                f"error={relative_error * 100:.1f}%"
            ),
            "target": "relative error <= 25%",
            "value": round(relative_error, 4),
            "threshold": 0.25,
            "details": {
                "avg_queue_length": round(avg_queue, 3),
                "arrival_rate_per_min": round(lambda_per_min, 3),
                "predicted_wait_min": round(avg_wait, 3),
                "lambda_times_wait": round(lambda_times_w, 3),
            },
        }

    def _compute_spec4_card(self, *, trend_points: list[TrendPoint]) -> dict[str, Any]:
        title = "Spec 4 - CI Half-Width Ratio"
        queue_values = [float(point.queue_length) for point in trend_points]
        n = len(queue_values)
        if n < 2:
            return {
                "title": title,
                "status": "insufficient_data",
                "current_value": "Need at least 2 data points.",
                "target": "ratio <= 30% with n>=10",
                "value": None,
                "threshold": 0.30,
            }

        mean_queue = statistics.mean(queue_values)
        std_dev = statistics.stdev(queue_values) if n >= 2 else 0.0
        half_width = 1.96 * std_dev / math.sqrt(n)
        if mean_queue <= 1e-6:
            ratio = 0.0 if half_width <= 1e-6 else float("inf")
        else:
            ratio = half_width / mean_queue

        if n < 10:
            status = "insufficient_data"
        else:
            status = "pass" if ratio <= 0.30 else "warn"

        ratio_text = "inf" if math.isinf(ratio) else f"{ratio * 100:.1f}%"
        return {
            "title": title,
            "status": status,
            "current_value": (
                f"half-width={half_width:.3f}, mean={mean_queue:.3f}, ratio={ratio_text}, n={n}"
            ),
            "target": "ratio <= 30% with n>=10",
            "value": None if math.isinf(ratio) else round(ratio, 4),
            "threshold": 0.30,
            "details": {
                "sample_count": n,
                "mean_queue": round(mean_queue, 3),
                "half_width": round(half_width, 3),
            },
        }

    @staticmethod
    def _coerce_int(value: Any, minimum: int, fallback: int | None) -> int | None:
        if value is None:
            return fallback
        coerced = int(value)
        if coerced < minimum:
            raise ValueError(f"value must be >= {minimum}")
        return coerced

    @staticmethod
    def _traffic_light(occupancy_pct: float) -> str:
        if occupancy_pct >= 85.0:
            return "red"
        if occupancy_pct >= 70.0:
            return "yellow"
        return "green"

    @staticmethod
    def _serialize_recommendation(recommendation: Recommendation | None) -> dict[str, Any]:
        if recommendation is None:
            return {
                "text": "No active recommendation.",
                "rationale": "Waiting for next trigger.",
                "score": None,
                "candidate_scores": {},
            }
        return {
            "text": recommendation.rationale,
            "rationale": recommendation.rationale,
            "score": round(recommendation.score, 3),
            "candidate_scores": {
                key: round(value, 3) for key, value in recommendation.candidate_scores.items()
            },
        }

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import heapq
import math

import numpy as np
import pandas as pd
from scipy import stats
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.stats import t


# =========================
# Data structures
# =========================

@dataclass
class Snapshot:
    timestamp: str
    queue_length: int
    waiting_trucks: List[Dict[str, Any]]
    unloading_trucks: List[Dict[str, Any]]
    docks: List[Dict[str, Any]]
    staging: List[Dict[str, Any]]
    staging_occupancy_units: int
    staging_capacity_units: int
    staging_ratio: float
    arrival_rate_per_hour: float
    service_stats: Dict[str, Dict[str, float]]
    resources: Dict[str, int]
    alerts: List[str]


@dataclass
class ForecastResult:
    baseline_rate_per_hour: float
    smoothed_rate_per_hour: float
    expected_arrivals: float
    scenarios: Dict[str, float]


@dataclass
class ActionPlan:
    name: str
    workers_by_dock: Dict[int, int]
    forklifts_by_dock: Dict[int, int]
    gate_release_factor: float
    staging_clearance_boost: float
    notes: str


@dataclass
class DESResult:
    avg_wait_minutes: float
    avg_time_in_system_minutes: float
    avg_queue_length: float
    avg_number_in_system: float
    dock_utilization: float
    staging_overflow_risk: float
    throughput: int
    effective_flow_rate_per_hour: float
    score: float
    replication_mean_tis: List[float]


# =========================
# Config defaults
# =========================

DEFAULTS: Dict[str, Any] = {
    "rolling_window_minutes": 30,
    "horizon_minutes": 60,
    "smoothing_alpha": 0.35,
    "surge_delta": 0.20,
    "risk_threshold": 0.85,
    "critical_threshold": 0.90,
    "default_staging_capacity": 100,
    "default_workers": 8,
    "default_forklifts": 2,
    "default_docks": 4,
    "max_unloaders_per_dock": 4,
    "replications": 10,
    "seed": 42,
}

# Floor-loaded trucks use workers inside the truck / staging pair.
# Palletized trucks are handled only by forklift in this model.
CLASS_CAPS = {
    "small": 2,
    "medium": 3,
    "large": 4,
}

# Reference service profiles used when history is missing.
# For floor-loaded trucks, means are calibrated around realistic staffing at class cap.
# For palletized trucks, exactly one forklift serves the truck; size only changes duration.
DEFAULT_SERVICE_PROFILES: Dict[str, Dict[str, float]] = {
    "small_floor": {"mean": 32.0, "std": 5.0, "reference_workers": 2},
    "medium_floor": {"mean": 48.0, "std": 7.0, "reference_workers": 3},
    "large_floor": {"mean": 70.0, "std": 10.0, "reference_workers": 4},
    "small_palletized": {"mean": 20.0, "std": 3.0, "reference_workers": 0},
    "medium_palletized": {"mean": 30.0, "std": 4.0, "reference_workers": 0},
    "large_palletized": {"mean": 42.0, "std": 6.0, "reference_workers": 0},
}

# Default inbound mix used when recent data is sparse.
# This favors floor-loaded trucks as requested.
DEFAULT_COMBINATION_PROBS: Dict[str, float] = {
    "small_floor": 0.24,
    "small_palletized": 0.06,
    "medium_floor": 0.35,
    "medium_palletized": 0.15,
    "large_floor": 0.12,
    "large_palletized": 0.08,
}

CANDIDATES = {
    "gamma": stats.gamma,
    "lognorm": stats.lognorm,
    "weibull_min": stats.weibull_min,
}

ACTION_NAMES = ["balanced", "prioritize_large", "clear_staging", "throttle_gate"]


# =========================
# Helpers
# =========================


def _to_df(rows: Optional[List[Dict[str, Any]]]) -> pd.DataFrame:
    return pd.DataFrame(rows or [])



def _to_dt(x: Any) -> pd.Timestamp:
    return pd.to_datetime(x)



def _safe_minutes(start_ts: Any, end_ts: datetime) -> float:
    if start_ts is None or start_ts == "":
        return 0.0
    dt = _to_dt(end_ts) - _to_dt(start_ts)
    return max(dt.total_seconds() / 60.0, 0.0)



def _rng(seed: int | None = None) -> np.random.Generator:
    return np.random.default_rng(seed)



def _combo_key(truck_class: str, load_type: str) -> str:
    return f"{truck_class}_{load_type}"



def _profile_for(key: str) -> Dict[str, float]:
    return DEFAULT_SERVICE_PROFILES[key]



def _truck_age_minutes(now: datetime, ts_value: Any) -> float:
    if ts_value is None or ts_value == "":
        return 0.0
    return max((_to_dt(now) - _to_dt(ts_value)).total_seconds() / 60.0, 0.0)



def _load_units_for_class(truck_class: str) -> int:
    return {"small": 8, "medium": 12, "large": 16}.get(truck_class, 12)


# =========================
# Input processing / state estimation
# =========================


def estimate_service_stats(service_history: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {
        key: {
            "mean": float(profile["mean"]),
            "std": float(profile["std"]),
            "n": 0,
            "reference_workers": int(profile["reference_workers"]),
        }
        for key, profile in DEFAULT_SERVICE_PROFILES.items()
    }

    if service_history.empty:
        return out

    h = service_history.copy()
    if "service_minutes" not in h.columns:
        h["service_minutes"] = (
            pd.to_datetime(h["end_ts"]) - pd.to_datetime(h["start_ts"])
        ).dt.total_seconds() / 60.0

    for (truck_class, load_type), grp in h.groupby(["truck_class", "load_type"]):
        key = _combo_key(str(truck_class), str(load_type))
        x = grp["service_minutes"].dropna().to_numpy(dtype=float)
        x = x[x > 0]
        if len(x) == 0:
            continue
        out[key] = {
            "mean": float(np.mean(x)),
            "std": float(max(np.std(x, ddof=0), 1.0)),
            "n": int(len(x)),
            "reference_workers": int(DEFAULT_SERVICE_PROFILES[key]["reference_workers"]),
        }
    return out



def fit_service_distributions(service_history: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {
        key: {
            "distribution": "normal_fallback",
            "params": None,
            "mean": float(profile["mean"]),
            "std": float(profile["std"]),
            "n": 0,
            "reference_workers": int(profile["reference_workers"]),
        }
        for key, profile in DEFAULT_SERVICE_PROFILES.items()
    }

    if service_history.empty:
        return results

    h = service_history.copy()
    if "service_minutes" not in h.columns:
        h["service_minutes"] = (
            pd.to_datetime(h["end_ts"]) - pd.to_datetime(h["start_ts"])
        ).dt.total_seconds() / 60.0

    for (truck_class, load_type), grp in h.groupby(["truck_class", "load_type"]):
        x = grp["service_minutes"].dropna().to_numpy(dtype=float)
        x = x[x > 0]
        key = _combo_key(str(truck_class), str(load_type))
        if len(x) < 8:
            results[key]["mean"] = float(np.mean(x)) if len(x) else results[key]["mean"]
            results[key]["std"] = float(max(np.std(x), 1.0)) if len(x) else results[key]["std"]
            results[key]["n"] = int(len(x))
            continue

        best_name = None
        best_params = None
        best_aic = float("inf")
        for name, dist in CANDIDATES.items():
            try:
                params = dist.fit(x, floc=0)
                loglik = np.sum(dist.logpdf(x, *params))
                aic = 2 * len(params) - 2 * loglik
                if aic < best_aic:
                    best_aic = aic
                    best_name = name
                    best_params = tuple(float(p) for p in params)
            except Exception:
                continue

        results[key] = {
            "distribution": best_name or "normal_fallback",
            "params": best_params,
            "mean": float(np.mean(x)),
            "std": float(max(np.std(x), 1.0)),
            "n": int(len(x)),
            "reference_workers": int(DEFAULT_SERVICE_PROFILES[key]["reference_workers"]),
        }
    return results



def build_snapshot_from_inputs(inputs: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Snapshot:
    cfg = {**DEFAULTS, **(config or {})}
    now = _to_dt(inputs.get("now", datetime.now())).to_pydatetime()

    waiting = _to_df(inputs.get("waiting_trucks"))
    unloading = _to_df(inputs.get("unloading_trucks"))
    staging = _to_df(inputs.get("staging_status"))
    supervisor = inputs.get("supervisor_input") or {}
    arrival_events = _to_df(inputs.get("arrival_history"))
    service_history = _to_df(inputs.get("service_history"))

    if waiting.empty:
        waiting = pd.DataFrame(columns=["truck_id", "truck_class", "load_type", "load_units", "gate_arrival_ts"])
    if unloading.empty:
        unloading = pd.DataFrame(columns=["truck_id", "truck_class", "load_type", "load_units", "service_start_ts", "current_dock_id", "gate_arrival_ts"])
    if staging.empty:
        staging = pd.DataFrame([
            {
                "zone_id": "S1",
                "occupancy_units": 0,
                "capacity_units": cfg["default_staging_capacity"],
                "occupancy_percent": 0.0,
            }
        ])

    for frame in [waiting, unloading]:
        if "load_units" not in frame.columns:
            frame["load_units"] = frame.get("truck_class", pd.Series([], dtype=object)).map(
                {"small": 8, "medium": 12, "large": 16}
            ).fillna(12)
        if "load_type" not in frame.columns:
            frame["load_type"] = "floor"
        if "truck_class" not in frame.columns:
            frame["truck_class"] = "medium"

    if not arrival_events.empty:
        if "timestamp" not in arrival_events.columns:
            raise ValueError("arrival_history must contain a timestamp field")
        since = now - timedelta(minutes=cfg["rolling_window_minutes"])
        arrival_events = arrival_events[pd.to_datetime(arrival_events["timestamp"]) >= pd.to_datetime(since)]
        arrival_rate = len(arrival_events) / cfg["rolling_window_minutes"] * 60.0
    else:
        arrival_rate = 0.0

    service_stats = estimate_service_stats(service_history)

    staging_occ = int(staging["occupancy_units"].sum()) if "occupancy_units" in staging.columns else 0
    staging_cap = int(staging["capacity_units"].sum()) if "capacity_units" in staging.columns else cfg["default_staging_capacity"]
    staging_ratio = float(staging_occ / staging_cap) if staging_cap > 0 else 0.0

    active_docks = int(supervisor.get("active_docks", cfg["default_docks"]))
    docks: List[Dict[str, Any]] = []
    unloading_map: Dict[int, Dict[str, Any]] = {}
    if not unloading.empty:
        for _, row in unloading.iterrows():
            dock_id = int(row.get("current_dock_id", 0) or 0)
            if dock_id > 0:
                unloading_map[dock_id] = row.to_dict()

    for dock_id in range(1, active_docks + 1):
        if dock_id in unloading_map:
            tr = unloading_map[dock_id]
            key = _combo_key(tr["truck_class"], tr["load_type"])
            base_mean = service_stats.get(key, {"mean": DEFAULT_SERVICE_PROFILES[key]["mean"]})["mean"]
            eta = max(base_mean - _safe_minutes(tr.get("service_start_ts"), now), 0.0)
            docks.append({
                "dock_id": dock_id,
                "status": "busy",
                "truck_id": tr.get("truck_id"),
                "truck_class": tr.get("truck_class"),
                "load_type": tr.get("load_type"),
                "eta_minutes": eta,
            })
        else:
            docks.append({
                "dock_id": dock_id,
                "status": "idle",
                "truck_id": None,
                "truck_class": None,
                "load_type": None,
                "eta_minutes": 0.0,
            })

    alerts: List[str] = []
    if staging_ratio >= cfg["critical_threshold"]:
        alerts.append("Staging near full")

    if active_docks > 0:
        mean_service = float(np.mean([v["mean"] for v in service_stats.values()])) if service_stats else 40.0
        mu = 60.0 / max(mean_service, 1e-6)
        rho = arrival_rate / max(active_docks * mu, 1e-6)
        if rho >= 0.95:
            alerts.append("High dock utilization risk")

    return Snapshot(
        timestamp=now.isoformat(),
        queue_length=int(len(waiting)),
        waiting_trucks=waiting.to_dict(orient="records"),
        unloading_trucks=unloading.to_dict(orient="records"),
        docks=docks,
        staging=staging.to_dict(orient="records"),
        staging_occupancy_units=staging_occ,
        staging_capacity_units=staging_cap,
        staging_ratio=staging_ratio,
        arrival_rate_per_hour=float(arrival_rate),
        service_stats=service_stats,
        resources={
            "workers": int(supervisor.get("available_workers", cfg["default_workers"])),
            "forklifts": int(supervisor.get("available_forklifts", cfg["default_forklifts"])),
            "active_docks": active_docks,
            "max_unloaders_per_dock": int(supervisor.get("max_unloaders_per_dock", cfg["max_unloaders_per_dock"])),
        },
        alerts=alerts,
    )


# =========================
# Forecasting
# =========================


def smooth_rate(current_rate: float, previous_rate: Optional[float] = None, alpha: float = DEFAULTS["smoothing_alpha"]) -> float:
    if previous_rate is None:
        return current_rate
    return alpha * current_rate + (1.0 - alpha) * previous_rate



def make_forecast(snapshot: Snapshot, previous_rate: Optional[float] = None, horizon_minutes: int = 60, config: Optional[Dict[str, Any]] = None) -> ForecastResult:
    cfg = {**DEFAULTS, **(config or {})}
    base = snapshot.arrival_rate_per_hour
    smoothed = smooth_rate(base, previous_rate, alpha=cfg["smoothing_alpha"])
    delta = cfg["surge_delta"]
    scenarios = {
        "low": max(smoothed * (1.0 - delta), 0.0),
        "baseline": smoothed,
        "high": smoothed * (1.0 + delta),
    }
    expected_arrivals = smoothed * horizon_minutes / 60.0
    return ForecastResult(
        baseline_rate_per_hour=base,
        smoothed_rate_per_hour=smoothed,
        expected_arrivals=expected_arrivals,
        scenarios=scenarios,
    )


# =========================
# OR feasibility model
# =========================


def _dock_candidates(snapshot: Snapshot) -> List[dict]:
    candidates = []
    queue = list(snapshot.waiting_trucks[: snapshot.resources["active_docks"]])
    for dock in snapshot.docks:
        if dock["status"] == "idle" and queue:
            next_truck = queue.pop(0)
            state = "candidate"
        elif dock["status"] == "busy":
            truck = next(
                (
                    t
                    for t in snapshot.unloading_trucks
                    if int(t.get("current_dock_id", 0) or 0) == dock["dock_id"]
                ),
                None,
            )
            next_truck = truck or {"truck_class": "medium", "load_type": "floor", "truck_id": None}
            state = "busy"
        else:
            next_truck = {"truck_class": "medium", "load_type": "floor", "truck_id": None}
            state = "empty"
        candidates.append({"dock_id": dock["dock_id"], "truck": next_truck, "state": state})
    return candidates



def solve_feasible_allocation(snapshot: Snapshot, plan_name: str = "balanced") -> ActionPlan:
    docks = _dock_candidates(snapshot)
    n = len(docks)
    W = snapshot.resources["workers"]
    F = snapshot.resources["forklifts"]
    max_per_dock = snapshot.resources["max_unloaders_per_dock"]

    c = np.zeros(2 * n)
    integrality = np.ones(2 * n, dtype=int)
    lb = np.zeros(2 * n)
    ub = np.array([max_per_dock] * n + [1] * n, dtype=float)

    A = []
    b_l = []
    b_u = []

    for i, item in enumerate(docks):
        truck = item["truck"]
        state = item["state"]
        truck_class = truck.get("truck_class", "medium")
        load_type = truck.get("load_type", "floor")
        has_real_truck = bool(truck.get("truck_id"))

        if load_type == "floor":
            cap = min(CLASS_CAPS.get(truck_class, max_per_dock), max_per_dock)
            ub[i] = cap
            if has_real_truck:
                lb[i] = 1.0
        else:
            # Palletized trucks are forklift-only in this model.
            ub[i] = 0.0
            lb[i] = 0.0

        if load_type == "palletized":
            c[n + i] = -1.4
        elif plan_name == "prioritize_large" and truck_class == "large":
            c[i] = -1.1
        elif plan_name == "clear_staging":
            c[i] = 0.15
        elif plan_name == "throttle_gate":
            c[i] = 0.05
        else:
            c[i] = -0.2

        # Busy palletized truck already in the system should keep one forklift assigned.
        if load_type == "palletized" and state == "busy" and has_real_truck:
            lb[n + i] = 1.0

    # fixed worker pool
    A.append([1.0] * n + [0.0] * n)
    b_l.append(0.0)
    b_u.append(float(W))

    # fixed forklift pool
    A.append([0.0] * n + [1.0] * n)
    b_l.append(0.0)
    b_u.append(float(F))

    # at most one forklift per dock is already handled by variable upper bound = 1.

    lc = LinearConstraint(np.array(A, dtype=float), np.array(b_l, dtype=float), np.array(b_u, dtype=float))
    res = milp(c=c, integrality=integrality, bounds=Bounds(lb, ub), constraints=lc)
    x = np.floor(res.x[:n] + 1e-6).astype(int) if res.success else np.zeros(n, dtype=int)
    f = np.floor(res.x[n:] + 1e-6).astype(int) if res.success else np.zeros(n, dtype=int)

    workers_by_dock = {docks[i]["dock_id"]: int(x[i]) for i in range(n)}
    forklifts_by_dock = {docks[i]["dock_id"]: int(f[i]) for i in range(n)}

    gate_release_factor = 1.0
    clearance_boost = 1.0
    notes = "Balanced feasible allocation. Floor-loaded trucks use workers; palletized trucks use one forklift."
    if plan_name == "clear_staging":
        gate_release_factor = 0.9
        clearance_boost = 1.3
        notes = "Conservative unloading to reduce staging pressure and clear staging faster."
    elif plan_name == "prioritize_large":
        notes = "Pre-position labor toward large floor-loaded trucks and keep forklift service for palletized trucks."
    elif plan_name == "throttle_gate":
        gate_release_factor = 0.8
        clearance_boost = 1.15
        notes = "Throttle gate release because staging or queue risk is high."

    return ActionPlan(
        name=plan_name,
        workers_by_dock=workers_by_dock,
        forklifts_by_dock=forklifts_by_dock,
        gate_release_factor=gate_release_factor,
        staging_clearance_boost=clearance_boost,
        notes=notes,
    )


# =========================
# DES
# =========================


def _sample_service_minutes(rng: np.random.Generator, key: str, service_fits: Dict[str, dict], workers: int) -> float:
    fit = service_fits.get(key)
    if fit and fit.get("distribution") and fit.get("params"):
        dist = getattr(stats, fit["distribution"])
        base = float(dist.rvs(*fit["params"], random_state=rng))
    else:
        mean = float(service_fits.get(key, {}).get("mean", DEFAULT_SERVICE_PROFILES[key]["mean"]))
        std = float(service_fits.get(key, {}).get("std", DEFAULT_SERVICE_PROFILES[key]["std"]))
        base = float(max(rng.normal(mean, std), 5.0))

    if key.endswith("_floor"):
        ref_workers = int(service_fits.get(key, {}).get("reference_workers", DEFAULT_SERVICE_PROFILES[key]["reference_workers"]))
        actual_workers = max(int(workers), 1)
        missing_workers = max(ref_workers - actual_workers, 0)
        penalty_factor = 1.0 + 0.22 * missing_workers
        return max(base * penalty_factor, 5.0)

    # Palletized trucks use only one forklift; no worker adjustment is applied.
    return max(base, 5.0)



def _infer_combo_probabilities(truck_mix: List[dict]) -> Dict[str, float]:
    prior_strength = 20.0
    counts = {key: DEFAULT_COMBINATION_PROBS[key] * prior_strength for key in DEFAULT_COMBINATION_PROBS}
    for tr in truck_mix:
        key = _combo_key(str(tr.get("truck_class", "medium")), str(tr.get("load_type", "floor")))
        if key in counts:
            counts[key] += 1.0
    total = sum(counts.values())
    return {k: v / total for k, v in counts.items()}



def _generate_forecast_arrivals(
    rng: np.random.Generator,
    rate_per_hour: float,
    horizon_minutes: int,
    truck_mix: List[dict],
    combo_probabilities_override: Optional[Dict[str, float]] = None,
) -> List[dict]:
    lam = max(rate_per_hour * horizon_minutes / 60.0, 0.0)
    n = int(rng.poisson(lam))
    if n == 0:
        return []

    combo_probs = combo_probabilities_override or _infer_combo_probabilities(truck_mix)
    combos = list(combo_probs.keys())
    probs = np.array([combo_probs[k] for k in combos], dtype=float)
    arrivals = sorted(float(x) for x in rng.uniform(0, horizon_minutes, size=n))

    out = []
    for i, arrival_min in enumerate(arrivals):
        key = str(rng.choice(combos, p=probs))
        truck_class, load_type = key.split("_", 1)
        out.append(
            {
                "truck_id": f"forecast_{i}",
                "arrival_minute": arrival_min,
                "truck_class": truck_class,
                "load_type": load_type,
                "load_units": _load_units_for_class(truck_class),
            }
        )
    return out



def simulate_action(
    snapshot: Snapshot,
    action: ActionPlan,
    scenario_rate_per_hour: float,
    service_fits: Dict[str, dict],
    horizon_minutes: int = 60,
    replications: int = 10,
    seed: int = 42,
    warmup_minutes: int = 0,
    combo_probabilities_override: Optional[Dict[str, float]] = None,
) -> DESResult:
    waits_all: List[float] = []
    tis_all: List[float] = []
    queue_lengths: List[float] = []
    measured_horizon = max(horizon_minutes - warmup_minutes, 1)
    avg_numbers_in_system: List[float] = []
    utilizations: List[float] = []
    overflow_flags: List[float] = []
    throughputs: List[int] = []
    replication_mean_tis: List[float] = []

    truck_mix = snapshot.waiting_trucks + snapshot.unloading_trucks
    base_clearance_per_min = max(snapshot.staging_capacity_units / 180.0, 0.3) * action.staging_clearance_boost
    now_dt = _to_dt(snapshot.timestamp).to_pydatetime()

    for rep in range(replications):
        rng = _rng(seed + rep)
        docks = [{"busy_until": 0.0, "busy_time": 0.0, "current_truck_id": None} for _ in range(snapshot.resources["active_docks"])]
        staging_occ = float(snapshot.staging_occupancy_units)
        staging_cap = float(snapshot.staging_capacity_units)
        overflow_count = 0
        completed = 0
        queue: List[Dict[str, Any]] = []
        event_heap: List[Any] = []

        rep_tis: List[float] = []

        # 1) Preserve true waiting age for trucks already in queue.
        for truck in snapshot.waiting_trucks:
            arrival_minute = -_truck_age_minutes(now_dt, truck.get("gate_arrival_ts"))
            queue.append(
                {
                    "truck_id": truck.get("truck_id"),
                    "arrival_minute": arrival_minute,
                    "truck_class": truck.get("truck_class", "medium"),
                    "load_type": truck.get("load_type", "floor"),
                    "load_units": int(truck.get("load_units", _load_units_for_class(truck.get("truck_class", "medium")))),
                }
            )
        queue.sort(key=lambda x: x["arrival_minute"])

        # 2) Create real departure events for trucks already unloading.
        for truck in snapshot.unloading_trucks:
            dock_id = int(truck.get("current_dock_id", 0) or 0)
            if dock_id < 1 or dock_id > len(docks):
                continue
            key = _combo_key(str(truck.get("truck_class", "medium")), str(truck.get("load_type", "floor")))
            workers_now = int(action.workers_by_dock.get(dock_id, 0)) if key.endswith("_floor") else 0
            remaining = max(float(next((d["eta_minutes"] for d in snapshot.docks if d["dock_id"] == dock_id), 0.0)), 0.1)
            service_age = _truck_age_minutes(now_dt, truck.get("service_start_ts"))
            gate_age = _truck_age_minutes(now_dt, truck.get("gate_arrival_ts")) if truck.get("gate_arrival_ts") else service_age
            start_minute = -service_age
            arrival_minute = -gate_age
            finish = remaining
            docks[dock_id - 1]["busy_until"] = finish
            docks[dock_id - 1]["busy_time"] += finish
            docks[dock_id - 1]["current_truck_id"] = truck.get("truck_id")
            heapq.heappush(
                event_heap,
                (
                    finish,
                    "departure_from_dock",
                    {
                        "truck": {
                            "truck_id": truck.get("truck_id"),
                            "arrival_minute": arrival_minute,
                            "truck_class": truck.get("truck_class", "medium"),
                            "load_type": truck.get("load_type", "floor"),
                            "load_units": int(truck.get("load_units", _load_units_for_class(truck.get("truck_class", "medium")))),
                        },
                        "dock_id": dock_id,
                        "start": start_minute,
                        "finish": finish,
                        "workers": workers_now,
                    },
                ),
            )

        forecast_arrivals = _generate_forecast_arrivals(
            rng,
            scenario_rate_per_hour * action.gate_release_factor,
            horizon_minutes,
            truck_mix,
            combo_probabilities_override=combo_probabilities_override,
        )
        for truck in forecast_arrivals:
            heapq.heappush(event_heap, (truck["arrival_minute"], "arrival", truck))

        q_time_accum = 0.0
        nsys_time_accum = 0.0
        last_t = 0.0
        dock_ids = list(range(1, len(docks) + 1))

        def in_service_count(now_min: float) -> int:
            return sum(1 for dock in docks if dock["busy_until"] > now_min + 1e-9)

        def try_start_service(now_min: float) -> None:
            nonlocal staging_occ, overflow_count
            for dock_id in dock_ids:
                dock = docks[dock_id - 1]
                if dock["busy_until"] > now_min + 1e-9:
                    continue
                if not queue:
                    continue

                truck = queue[0]
                workers = int(action.workers_by_dock.get(dock_id, 0))
                forklift = int(action.forklifts_by_dock.get(dock_id, 0))

                if truck["load_type"] == "palletized":
                    if forklift < 1:
                        continue
                    workers = 0
                else:
                    workers = max(workers, 1)

                queue.pop(0)
                key = _combo_key(truck["truck_class"], truck["load_type"])
                service = _sample_service_minutes(rng, key, service_fits, workers)

                projected_staging = staging_occ + truck["load_units"]
                if projected_staging > staging_cap:
                    shortage = projected_staging - staging_cap
                    service += shortage / max(base_clearance_per_min, 0.1)
                    overflow_count += 1

                finish = now_min + service
                heapq.heappush(
                    event_heap,
                    (
                        finish,
                        "departure_from_dock",
                        {
                            "truck": truck,
                            "dock_id": dock_id,
                            "start": now_min,
                            "finish": finish,
                            "workers": workers,
                        },
                    ),
                )
                dock["busy_until"] = finish
                dock["busy_time"] += service
                dock["current_truck_id"] = truck.get("truck_id")

        try_start_service(0.0)

        while event_heap:
            event_time, event_type, payload = heapq.heappop(event_heap)
            if event_time > horizon_minutes:
                interval_start = max(last_t, warmup_minutes)
                interval_end = horizon_minutes
                dt_measured = max(interval_end - interval_start, 0.0)
                q_time_accum += len(queue) * dt_measured
                nsys_time_accum += (len(queue) + in_service_count(last_t)) * dt_measured
                break

            dt = max(event_time - last_t, 0.0)
            staging_occ = max(staging_occ - base_clearance_per_min * dt, 0.0)
            interval_start = max(last_t, warmup_minutes)
            interval_end = min(event_time, horizon_minutes)
            dt_measured = max(interval_end - interval_start, 0.0)
            q_time_accum += len(queue) * dt_measured
            nsys_time_accum += (len(queue) + in_service_count(last_t)) * dt_measured
            last_t = event_time

            if event_type == "arrival":
                queue.append(payload)
                queue.sort(key=lambda x: x["arrival_minute"])
                try_start_service(event_time)
            elif event_type == "departure_from_dock":
                truck = payload["truck"]
                staging_occ = min(staging_occ + truck["load_units"], staging_cap)
                wait = max(payload["start"] - truck["arrival_minute"], 0.0)
                tis = max(payload["finish"] - truck["arrival_minute"], 0.0)
                if payload["finish"] >= warmup_minutes:
                    waits_all.append(wait)
                    tis_all.append(tis)
                    rep_tis.append(tis)
                    completed += 1
                dock_id = int(payload["dock_id"])
                if 1 <= dock_id <= len(docks):
                    docks[dock_id - 1]["current_truck_id"] = None
                try_start_service(event_time)

        avg_queue = q_time_accum / max(measured_horizon, 1e-6)
        avg_nsys = nsys_time_accum / max(measured_horizon, 1e-6)
        util = sum(d["busy_time"] for d in docks) / max(len(docks) * horizon_minutes, 1e-6)
        queue_lengths.append(avg_queue)
        avg_numbers_in_system.append(avg_nsys)
        utilizations.append(min(util, 1.0))
        overflow_flags.append(1.0 if overflow_count > 0 else 0.0)
        throughputs.append(completed)
        replication_mean_tis.append(float(np.mean(rep_tis)) if rep_tis else 0.0)

    avg_wait = float(np.mean(waits_all)) if waits_all else 0.0
    avg_tis = float(np.mean(tis_all)) if tis_all else 0.0
    avg_q = float(np.mean(queue_lengths)) if queue_lengths else 0.0
    avg_nsys = float(np.mean(avg_numbers_in_system)) if avg_numbers_in_system else 0.0
    avg_util = float(np.mean(utilizations)) if utilizations else 0.0
    overflow_risk = float(np.mean(overflow_flags)) if overflow_flags else 0.0
    throughput = int(np.mean(throughputs)) if throughputs else 0
    effective_flow_rate = float((np.mean(throughputs) if throughputs else 0.0) * 60.0 / max(measured_horizon, 1e-6))
    score = avg_tis + 10.0 * overflow_risk + 4.0 * max(avg_util - 0.95, 0.0)

    return DESResult(
        avg_wait_minutes=avg_wait,
        avg_time_in_system_minutes=avg_tis,
        avg_queue_length=avg_q,
        avg_number_in_system=avg_nsys,
        dock_utilization=avg_util,
        staging_overflow_risk=overflow_risk,
        throughput=throughput,
        effective_flow_rate_per_hour=effective_flow_rate,
        score=score,
        replication_mean_tis=replication_mean_tis,
    )


# =========================
# Verification of ISE specs
# =========================


def littles_law_check(arrival_rate_per_hour: float, avg_time_in_system_minutes: float, avg_number_in_system: float) -> Dict[str, Any]:
    lam_per_min = arrival_rate_per_hour / 60.0
    lhs = float(avg_number_in_system)
    rhs = float(lam_per_min * avg_time_in_system_minutes)
    denom = max(rhs, 1e-6)
    rel_error = abs(lhs - rhs) / denom
    return {
        "lhs_avg_number_in_system": lhs,
        "rhs_lambda_times_W": rhs,
        "lambda_per_hour_used": float(arrival_rate_per_hour),
        "relative_error": float(rel_error),
        "target_max_error": 0.10,
        "pass": bool(rel_error <= 0.10),
    }



def ci_halfwidth_ratio(replication_means: List[float]) -> Dict[str, Any]:
    x = np.asarray(replication_means, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return {
            "n_replications": int(len(x)),
            "mean": float(x.mean()) if len(x) else 0.0,
            "half_width": 0.0,
            "ratio": 0.0,
            "target_max_ratio": 0.20,
            "pass": False,
        }

    mean = float(np.mean(x))
    s = float(np.std(x, ddof=1))
    hw = float(t.ppf(0.975, len(x) - 1) * s / math.sqrt(len(x)))
    ratio = hw / max(mean, 1e-6)
    return {
        "n_replications": int(len(x)),
        "mean": mean,
        "half_width": hw,
        "ratio": float(ratio),
        "target_max_ratio": 0.20,
        "pass": bool(ratio <= 0.20),
    }




def _steady_state_verification(snapshot: Snapshot, service_fits: Dict[str, dict], config: Optional[Dict[str, Any]] = None) -> DESResult:
    cfg = {**DEFAULTS, **(config or {})}
    active_docks = max(snapshot.resources["active_docks"], 1)
    workers_total = max(snapshot.resources["workers"], active_docks)
    workers_per_dock = max(workers_total // active_docks, 1)
    key = "medium_floor"
    base_mean = float(service_fits[key]["mean"])
    ref_workers = int(service_fits[key].get("reference_workers", DEFAULT_SERVICE_PROFILES[key]["reference_workers"]))
    mean_service = base_mean * (1.0 + 0.22 * max(ref_workers - workers_per_dock, 0))
    service_rate_per_dock = 60.0 / max(mean_service, 1e-6)
    target_lambda = 0.75 * active_docks * service_rate_per_dock

    steady_snapshot = Snapshot(
        timestamp=snapshot.timestamp,
        queue_length=0,
        waiting_trucks=[],
        unloading_trucks=[],
        docks=[{"dock_id": i + 1, "status": "idle", "truck_id": None, "truck_class": None, "load_type": None, "eta_minutes": 0.0} for i in range(active_docks)],
        staging=snapshot.staging,
        staging_occupancy_units=0,
        staging_capacity_units=max(snapshot.staging_capacity_units, 120),
        staging_ratio=0.0,
        arrival_rate_per_hour=target_lambda,
        service_stats=snapshot.service_stats,
        resources=snapshot.resources,
        alerts=[],
    )
    action = ActionPlan(
        name="verification_balanced",
        workers_by_dock={i + 1: workers_per_dock for i in range(active_docks)},
        forklifts_by_dock={i + 1: 0 for i in range(active_docks)},
        gate_release_factor=1.0,
        staging_clearance_boost=1.0,
        notes="steady-state verification action",
    )
    return simulate_action(
        snapshot=steady_snapshot,
        action=action,
        scenario_rate_per_hour=target_lambda,
        service_fits=service_fits,
        horizon_minutes=720,
        replications=max(cfg.get("replications", 10), 16),
        seed=cfg.get("seed", 42) + 500,
        warmup_minutes=240,
        combo_probabilities_override={"medium_floor": 1.0},
    )


def build_verification_bundle(snapshot: Snapshot, des_result: DESResult, service_fits: Dict[str, dict], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    steady_state_result = _steady_state_verification(snapshot, service_fits=service_fits, config=config)
    spec3 = littles_law_check(
        arrival_rate_per_hour=steady_state_result.effective_flow_rate_per_hour,
        avg_time_in_system_minutes=steady_state_result.avg_time_in_system_minutes,
        avg_number_in_system=steady_state_result.avg_number_in_system,
    )
    spec4 = ci_halfwidth_ratio(steady_state_result.replication_mean_tis)
    return {
        "spec_3_littles_law": spec3,
        "spec_4_ci_halfwidth": spec4,
        "dashboard_cards": [
            {
                "title": "Spec 3 - Little's Law",
                "status": "PASS" if spec3["pass"] else "FAIL",
                "value": round(100.0 * spec3["relative_error"], 2),
                "unit": "% error",
                "target": "<= 10%",
            },
            {
                "title": "Spec 4 - CI Half-Width Ratio",
                "status": "PASS" if spec4["pass"] else "FAIL",
                "value": round(100.0 * spec4["ratio"], 2),
                "unit": "% of mean",
                "target": "<= 20%",
            },
        ],
        "steady_state_reference": asdict(steady_state_result),
    }


# =========================
# Full ISE cycle
# =========================


def run_ise_cycle(inputs: Dict[str, Any], previous_rate: Optional[float] = None, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = {**DEFAULTS, **(config or {})}

    snapshot = build_snapshot_from_inputs(inputs, cfg)
    forecast = make_forecast(snapshot, previous_rate=previous_rate, horizon_minutes=cfg["horizon_minutes"], config=cfg)

    service_history = _to_df(inputs.get("service_history"))
    service_fits = fit_service_distributions(service_history)
    for key, val in snapshot.service_stats.items():
        service_fits.setdefault(key, val)
        service_fits[key]["reference_workers"] = DEFAULT_SERVICE_PROFILES[key]["reference_workers"]

    evaluations: List[Dict[str, Any]] = []
    for action_name in ACTION_NAMES:
        action = solve_feasible_allocation(snapshot, plan_name=action_name)
        scenario_results: Dict[str, Any] = {}
        scores: List[float] = []
        baseline_des: Optional[DESResult] = None
        for scenario_name, rate in forecast.scenarios.items():
            des_result = simulate_action(
                snapshot=snapshot,
                action=action,
                scenario_rate_per_hour=rate,
                service_fits=service_fits,
                horizon_minutes=cfg["horizon_minutes"],
                replications=cfg["replications"],
                seed=cfg["seed"],
            )
            if scenario_name == "baseline":
                baseline_des = des_result
            scenario_results[scenario_name] = asdict(des_result)
            scores.append(des_result.score)

        if baseline_des is None:
            raise RuntimeError("Baseline scenario result is missing")

        verification = build_verification_bundle(snapshot, baseline_des, service_fits=service_fits, config=cfg)
        evaluations.append(
            {
                "action": asdict(action),
                "baseline": scenario_results["baseline"],
                "scenarios": scenario_results,
                "verification": verification,
                "robust_score": float(max(scores)),
            }
        )

    evaluations.sort(key=lambda x: (x["robust_score"], x["baseline"]["avg_time_in_system_minutes"]))
    best = evaluations[0]

    rationale_parts = [best["action"]["notes"]]
    if snapshot.staging_ratio >= cfg["critical_threshold"]:
        rationale_parts.append("Staging is critical, so protective actions were prioritized.")
    elif snapshot.queue_length >= snapshot.resources["active_docks"]:
        rationale_parts.append("Queue pressure is high relative to active docks.")
    else:
        rationale_parts.append("No severe trigger was active, so the lowest-delay feasible action was selected.")

    best_recommendation = {
        "timestamp": snapshot.timestamp,
        "action_name": best["action"]["name"],
        "action_payload": best["action"],
        "expected_wait_minutes": best["baseline"]["avg_wait_minutes"],
        "expected_queue_length": best["baseline"]["avg_queue_length"],
        "expected_time_in_system_minutes": best["baseline"]["avg_time_in_system_minutes"],
        "expected_utilization": best["baseline"]["dock_utilization"],
        "staging_risk": best["baseline"]["staging_overflow_risk"],
        "score": best["robust_score"],
        "rationale": " ".join(rationale_parts),
    }

    return {
        "snapshot": asdict(snapshot),
        "forecast": asdict(forecast),
        "best_recommendation": best_recommendation,
        "verification": best["verification"],
        "evaluations": evaluations,
    }


# =========================
# Sample usage
# =========================


def _service_history_rows(now: datetime, key: str, durations: List[float], start_offset: int) -> List[Dict[str, Any]]:
    truck_class, load_type = key.split("_", 1)
    rows: List[Dict[str, Any]] = []
    for i, dur in enumerate(durations):
        end_ts = now - timedelta(minutes=start_offset - i * 9)
        start_ts = end_ts - timedelta(minutes=float(dur))
        rows.append(
            {
                "truck_class": truck_class,
                "load_type": load_type,
                "start_ts": start_ts.isoformat(),
                "end_ts": end_ts.isoformat(),
            }
        )
    return rows



def _sample_inputs() -> Dict[str, Any]:
    now = datetime.now()
    service_history: List[Dict[str, Any]] = []
    service_history += _service_history_rows(now, "small_floor", [31, 34, 30, 33, 32, 29, 35, 31], 420)
    service_history += _service_history_rows(now, "medium_floor", [47, 49, 50, 46, 52, 48, 47, 51], 380)
    service_history += _service_history_rows(now, "large_floor", [71, 68, 74, 72, 69, 76, 70, 73], 340)
    service_history += _service_history_rows(now, "small_palletized", [19, 20, 22, 18, 21, 19, 20, 21], 300)
    service_history += _service_history_rows(now, "medium_palletized", [29, 31, 30, 28, 32, 29, 30, 31], 260)
    service_history += _service_history_rows(now, "large_palletized", [41, 44, 43, 40, 42, 45, 41, 43], 220)

    return {
        "now": now.isoformat(),
        "waiting_trucks": [
            {
                "truck_id": "T101",
                "truck_class": "large",
                "load_type": "floor",
                "load_units": 16,
                "gate_arrival_ts": (now - timedelta(minutes=26)).isoformat(),
            },
            {
                "truck_id": "T102",
                "truck_class": "medium",
                "load_type": "palletized",
                "load_units": 12,
                "gate_arrival_ts": (now - timedelta(minutes=18)).isoformat(),
            },
            {
                "truck_id": "T103",
                "truck_class": "small",
                "load_type": "floor",
                "load_units": 8,
                "gate_arrival_ts": (now - timedelta(minutes=11)).isoformat(),
            },
        ],
        "unloading_trucks": [
            {
                "truck_id": "T090",
                "truck_class": "medium",
                "load_type": "floor",
                "load_units": 12,
                "gate_arrival_ts": (now - timedelta(minutes=42)).isoformat(),
                "service_start_ts": (now - timedelta(minutes=14)).isoformat(),
                "current_dock_id": 1,
            },
            {
                "truck_id": "T091",
                "truck_class": "large",
                "load_type": "palletized",
                "load_units": 16,
                "gate_arrival_ts": (now - timedelta(minutes=34)).isoformat(),
                "service_start_ts": (now - timedelta(minutes=9)).isoformat(),
                "current_dock_id": 2,
            },
        ],
        "staging_status": [
            {"zone_id": "S1", "occupancy_units": 24, "capacity_units": 30, "occupancy_percent": 80.0},
            {"zone_id": "S2", "occupancy_units": 20, "capacity_units": 30, "occupancy_percent": 66.7},
            {"zone_id": "S3", "occupancy_units": 16, "capacity_units": 20, "occupancy_percent": 80.0},
            {"zone_id": "S4", "occupancy_units": 12, "capacity_units": 20, "occupancy_percent": 60.0},
        ],
        "supervisor_input": {
            "available_workers": 8,
            "available_forklifts": 2,
            "active_docks": 4,
            "max_unloaders_per_dock": 4,
        },
        "arrival_history": [
            {"timestamp": (now - timedelta(minutes=29)).isoformat()},
            {"timestamp": (now - timedelta(minutes=26)).isoformat()},
            {"timestamp": (now - timedelta(minutes=23)).isoformat()},
            {"timestamp": (now - timedelta(minutes=20)).isoformat()},
            {"timestamp": (now - timedelta(minutes=16)).isoformat()},
            {"timestamp": (now - timedelta(minutes=13)).isoformat()},
            {"timestamp": (now - timedelta(minutes=9)).isoformat()},
            {"timestamp": (now - timedelta(minutes=6)).isoformat()},
        ],
        "service_history": service_history,
    }


if __name__ == "__main__":
    import json

    result = run_ise_cycle(_sample_inputs())
    print(json.dumps(result, indent=2))

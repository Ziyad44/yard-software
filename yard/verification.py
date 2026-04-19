"""ISE-style verification metrics derived from simulation outputs."""

from __future__ import annotations

import math
import statistics
from typing import Any

EPSILON = 1e-6
WARN_MULTIPLIER = 1.25


def _status_from_ratio(*, value: float, threshold: float, invalid: bool = False) -> str:
    limit = max(float(threshold), EPSILON)
    if invalid or not math.isfinite(value):
        return "fail"
    if value <= limit:
        return "pass"
    if value <= limit * WARN_MULTIPLIER:
        return "warn"
    return "fail"


def _format_threshold_percent(threshold: float) -> str:
    return f"{max(float(threshold), 0.0) * 100.0:.0f}%"


def littles_law_check(
    *,
    throughput_rate_trucks_per_min: float,
    avg_time_in_system_minutes: float,
    avg_number_in_system: float,
    threshold: float,
) -> dict[str, Any]:
    lambda_per_min = max(float(throughput_rate_trucks_per_min), 0.0)
    w_minutes = max(float(avg_time_in_system_minutes), 0.0)
    l_trucks = max(float(avg_number_in_system), 0.0)
    lambda_w = lambda_per_min * w_minutes
    error_ratio = abs(l_trucks - lambda_w) / max(lambda_w, EPSILON)
    error_percent = error_ratio * 100.0
    threshold_ratio = max(float(threshold), 0.0)
    threshold_percent = threshold_ratio * 100.0
    is_pass = math.isfinite(error_ratio) and error_ratio <= threshold_ratio
    status = "pass" if is_pass else "fail"
    pass_fail = "PASS" if is_pass else "FAIL"
    return {
        # Machine-friendly keys (backward-compatible aliases kept where possible).
        "lhs_avg_number_in_system": l_trucks,
        "rhs_lambda_times_W": lambda_w,
        "relative_error": error_ratio,
        "relative_error_percent": error_percent,
        "target_max_error": threshold_ratio,
        "throughput_rate_trucks_per_min_used": lambda_per_min,
        "avg_time_in_system_minutes_used": w_minutes,
        # Keep these legacy aliases to avoid downstream breakage.
        "arrival_rate_per_min_used": lambda_per_min,
        "arrival_rate_per_hour_used": lambda_per_min * 60.0,
        # Excel/photo-aligned labels and values.
        "Time-average number in system, L (trucks)": l_trucks,
        "Throughput rate, lambda (trucks/min)": lambda_per_min,
        "Average time in system, W (min)": w_minutes,
        "Computed lambda*W": lambda_w,
        "Relative error %": error_percent,
        "Target (<=)": threshold_percent,
        "PASS / FAIL": pass_fail,
        "status": status,
        "pass": is_pass,
    }


def ci_half_width_ratio(
    *,
    replication_means: list[float],
    threshold: float,
) -> dict[str, Any]:
    values = [float(value) for value in replication_means if math.isfinite(float(value))]
    n = len(values)
    if n < 2:
        mean = values[0] if values else 0.0
        return {
            "n_replications": n,
            "mean": mean,
            "half_width": 0.0,
            "ratio": 0.0,
            "target_max_ratio": threshold,
            "status": "insufficient_data",
            "insufficient_data": True,
            "reason": "Need at least 2 replications to compute CI half-width.",
            "pass": False,
        }

    mean = statistics.mean(values)
    std_dev = statistics.stdev(values)
    # Normal approximation (z=1.96) to avoid external dependencies.
    half_width = 1.96 * std_dev / math.sqrt(n)
    ratio = half_width / max(abs(mean), EPSILON)
    near_zero_mean = abs(mean) <= EPSILON
    invalid = not math.isfinite(ratio) or (near_zero_mean and half_width > EPSILON)
    status = _status_from_ratio(value=ratio, threshold=threshold, invalid=invalid)
    return {
        "n_replications": n,
        "mean": mean,
        "half_width": half_width,
        "ratio": ratio,
        "target_max_ratio": threshold,
        "status": status,
        "insufficient_data": False,
        "pass": status == "pass",
    }


def build_verification_bundle(
    *,
    throughput_rate_trucks_per_min: float,
    avg_time_in_system_minutes: float,
    avg_number_in_system: float,
    replication_means: list[float],
    littles_law_threshold: float,
    ci_threshold: float,
) -> dict[str, Any]:
    spec3 = littles_law_check(
        throughput_rate_trucks_per_min=throughput_rate_trucks_per_min,
        avg_time_in_system_minutes=avg_time_in_system_minutes,
        avg_number_in_system=avg_number_in_system,
        threshold=littles_law_threshold,
    )
    spec4 = ci_half_width_ratio(
        replication_means=replication_means,
        threshold=ci_threshold,
    )
    spec4_target_label = f"<= {_format_threshold_percent(ci_threshold)} of mean"
    spec3_ratio = float(spec3.get("relative_error", 0.0))
    spec4_ratio = float(spec4.get("ratio", 0.0))
    spec3_value_pct = round(spec3_ratio * 100.0, 2) if math.isfinite(spec3_ratio) else None
    spec4_value_pct = round(spec4_ratio * 100.0, 2) if math.isfinite(spec4_ratio) else None
    spec3_status = str(spec3.get("PASS / FAIL", "FAIL")).upper()
    spec4_status = str(spec4.get("status", "warn")).upper()
    return {
        "spec_3_littles_law": spec3,
        "spec_4_ci_halfwidth": spec4,
        "dashboard_cards": [
            {
                "title": "Spec 3 - Little's Law",
                "status": spec3_status,
                "value": spec3_value_pct,
                "unit": "% error",
                "target": f"Target (<=): {_format_threshold_percent(littles_law_threshold)}",
            },
            {
                "title": "Spec 4 - CI Half-Width Ratio",
                "status": spec4_status,
                "value": spec4_value_pct,
                "unit": "% of mean",
                "target": spec4_target_label,
            },
        ],
    }

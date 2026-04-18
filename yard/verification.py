"""ISE-style verification metrics derived from simulation outputs."""

from __future__ import annotations

import math
import statistics
from typing import Any


def littles_law_check(
    *,
    arrival_rate_per_hour: float,
    avg_time_in_system_minutes: float,
    avg_number_in_system: float,
    threshold: float,
) -> dict[str, Any]:
    lam_per_min = max(float(arrival_rate_per_hour), 0.0) / 60.0
    lhs = max(float(avg_number_in_system), 0.0)
    rhs = lam_per_min * max(float(avg_time_in_system_minutes), 0.0)
    baseline = max(lhs, rhs, 1e-6)
    relative_error = abs(lhs - rhs) / baseline
    return {
        "lhs_avg_number_in_system": lhs,
        "rhs_lambda_times_W": rhs,
        "arrival_rate_per_hour_used": float(arrival_rate_per_hour),
        "relative_error": relative_error,
        "target_max_error": threshold,
        "pass": relative_error <= threshold,
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
            "pass": False,
        }

    mean = statistics.mean(values)
    std_dev = statistics.stdev(values)
    # Normal approximation (z=1.96) to avoid external dependencies.
    half_width = 1.96 * std_dev / math.sqrt(n)
    ratio = half_width / max(abs(mean), 1e-6)
    return {
        "n_replications": n,
        "mean": mean,
        "half_width": half_width,
        "ratio": ratio,
        "target_max_ratio": threshold,
        "pass": ratio <= threshold,
    }


def build_verification_bundle(
    *,
    arrival_rate_per_hour: float,
    avg_time_in_system_minutes: float,
    avg_number_in_system: float,
    replication_means: list[float],
    littles_law_threshold: float,
    ci_threshold: float,
) -> dict[str, Any]:
    spec3 = littles_law_check(
        arrival_rate_per_hour=arrival_rate_per_hour,
        avg_time_in_system_minutes=avg_time_in_system_minutes,
        avg_number_in_system=avg_number_in_system,
        threshold=littles_law_threshold,
    )
    spec4 = ci_half_width_ratio(
        replication_means=replication_means,
        threshold=ci_threshold,
    )
    return {
        "spec_3_littles_law": spec3,
        "spec_4_ci_halfwidth": spec4,
        "dashboard_cards": [
            {
                "title": "Spec 3 - Little's Law",
                "status": "PASS" if spec3["pass"] else "WARN",
                "value": round(spec3["relative_error"] * 100.0, 2),
                "unit": "% error",
                "target": f"<= {round(littles_law_threshold * 100.0, 1)}%",
            },
            {
                "title": "Spec 4 - CI Half-Width Ratio",
                "status": "PASS" if spec4["pass"] else "WARN",
                "value": round(spec4["ratio"] * 100.0, 2),
                "unit": "% of mean",
                "target": f"<= {round(ci_threshold * 100.0, 1)}%",
            },
        ],
    }


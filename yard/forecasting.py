"""ISE-style arrival forecasting utilities for minute-step yard simulation."""

from __future__ import annotations

from .config import YardConfig
from .models import ForecastResult, YardState


def _observed_arrivals_in_window(state: YardState, window_minutes: int) -> tuple[int, int]:
    if window_minutes <= 0:
        return 0, 1
    now = state.now_minute
    start_minute = now - window_minutes + 1
    observed = [
        arrivals
        for minute, arrivals in state.arrival_history
        if minute >= start_minute
    ]
    observed_arrivals = sum(observed)
    effective_window = max(min(window_minutes, now + 1), 1)
    return observed_arrivals, effective_window


def build_forecast(state: YardState, config: YardConfig) -> ForecastResult:
    """
    Build low/baseline/high arrival scenarios from observed minute arrivals.

    If history is sparse, fall back to configured baseline arrival rate.
    """
    observed_arrivals, effective_window = _observed_arrivals_in_window(state, config.forecast_window_minutes)
    if observed_arrivals <= 0:
        baseline_rate = max(float(config.arrival_rate_per_hour), 0.0)
    else:
        baseline_rate = observed_arrivals * (60.0 / effective_window)

    previous_smoothed = state.kpi_cache.get("smoothed_arrival_rate_per_hour")
    previous_minute = state.kpi_cache.get("smoothed_arrival_rate_minute")
    if previous_smoothed is None:
        smoothed_rate = baseline_rate
    elif previous_minute is not None and int(previous_minute) >= state.now_minute:
        # Idempotent behavior: repeated forecast calls within the same minute
        # return the same smoothed value.
        smoothed_rate = float(previous_smoothed)
    else:
        alpha = max(min(float(config.forecast_smoothing_alpha), 1.0), 0.0)
        smoothed_rate = alpha * baseline_rate + (1.0 - alpha) * float(previous_smoothed)

    delta = max(float(config.forecast_scenario_delta), 0.0)
    scenarios = {
        "low": max(smoothed_rate * (1.0 - delta), 0.0),
        "baseline": max(smoothed_rate, 0.0),
        "high": max(smoothed_rate * (1.0 + delta), 0.0),
    }
    expected_arrivals = scenarios["baseline"] * max(config.lookahead_horizon_minutes, 0) / 60.0

    return ForecastResult(
        baseline_rate_per_hour=baseline_rate,
        smoothed_rate_per_hour=smoothed_rate,
        expected_arrivals=expected_arrivals,
        scenarios=scenarios,
        window_minutes=effective_window,
        observed_arrivals=observed_arrivals,
    )

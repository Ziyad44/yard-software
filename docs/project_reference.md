# Project Reference: Smart Truck Yard & Dock Queue Management

## 1. Purpose and Scope

This repository contains a **version-1 (v1) smart yard backend** plus a local operations dashboard.

The active system is a discrete-time simulation and decision-support loop for truck unloading at docks:

- Simulates minute-by-minute yard behavior.
- Detects operational triggers.
- Generates resource-allocation recommendations.
- Lets a supervisor apply or reject recommendations.
- Exposes a local HTTP dashboard/API for operations visibility.

Important scope distinction:

- `yard/` = **active v1 implementation**.
- `ise_engine.py` + `sample_output.json` = **legacy reference implementation/artifact**, not used by active runtime.

---

## 2. High-Level Architecture

Active v1 architecture is modular and state-centric:

1. `yard/config.py` defines parameters.
2. `yard/models.py` defines dataclasses for state, actions, triggers, and snapshots.
3. `yard/simulation.py` advances live state one minute and emits triggers.
4. `yard/recommendation.py` builds candidate actions, simulates lookahead outcomes, and selects the best action.
5. `yard/engine.py` orchestrates one full minute cycle and snapshot generation.
6. `yard/dashboard_runtime.py` wraps the engine with dashboard-specific behavior and payload shaping.
7. `yard/dashboard_server.py` provides HTTP endpoints and embedded single-page UI.
8. `yard/demo_runner.py` runs a CLI simulation demonstration.

Core pattern: **single source of truth = `YardState`**.

---

## 3. Execution Flows (Input -> Output)

## 3.1 Backend Minute Cycle

Primary cycle (`engine.run_minute_cycle`):

1. `simulation.simulate_one_minute` updates state:
   - Advances clock by `config.time_step_minutes`.
   - Generates arrivals (Poisson).
   - Updates each dock’s unloading/clearing physics.
   - Dispatches queued trucks to fully free docks.
   - Detects staging-threshold and review-timer triggers.
2. If any triggers exist, `engine.recommend_on_triggers` runs recommendation.
3. `engine.refresh_kpi_cache` runs short lookahead simulation for KPI estimates.
4. Returns:
   - `list[TriggerEvent]`
   - `Recommendation | None`

## 3.2 Supervisor Decision Flow

When recommendation exists:

1. Supervisor chooses:
   - Apply recommendation (`runtime.apply_recommendation` / `/api/recommendation/apply`)
   - Keep current plan (`runtime.keep_current_plan` / `/api/recommendation/keep`)
2. Chosen action becomes live via `engine.apply_action`.
3. Next minute cycle uses updated assignments and gate-hold flag.

## 3.3 Dashboard/API Flow

HTTP entry points in `dashboard_server.py`:

- `GET /` or `/index.html` -> embedded HTML UI.
- `GET /api/state` -> current runtime payload.
- `POST /api/step` -> step simulation by `minutes`.
- `POST /api/supervisor` -> update resource and dock controls.
- `POST /api/recommendation/apply` -> apply selected recommendation.
- `POST /api/recommendation/keep` -> keep current plan.

All non-2xx error handling is JSON:

- `400` for invalid values / invalid JSON.
- `404` for unknown path.

---

## 4. Core Data Model

Defined in `yard/models.py`.

- `Truck`: per-truck lifecycle state (`remaining_load_units`, dock assignment, timestamps).
- `StagingAreaState`: per-dock staging occupancy/capacity + hysteresis flags.
  - includes `load_family` metadata used for load-type-exclusive clearing when no active truck object is attached.
- `DockState`: dock activity, assigned resources, current truck, phase logic.
- `ResourcePool`: total vs assigned resources with idle computed properties.
- `TriggerEvent`: event that gates recommendation execution.
- `Action`: supervisor-facing assignment and gate-hold payload.
- `ActionEvaluation`: predicted metrics + score for candidate actions.
- `Recommendation`: selected action + rationale + candidate scores.
- `DockSummary` and `SystemSnapshot`: dashboard-friendly output structures.
- `YardState`: global mutable system state container (queue, docks, resources, review scheduling, KPI cache, recommendation memory).

Key enum/literal domains:

- Truck types: `small|medium|large` x `floor|palletized` (encoded as strings like `medium_floor`).
- Trigger types: `dock_freed`, `staging_threshold`, `review_timer`.

---

## 5. Simulation Logic (Minute Physics)

Implemented in `yard/simulation.py`.

## 5.1 Arrival Generation

- Per-minute arrivals sampled from Poisson with  
  `lambda = arrival_rate_per_hour * time_step_minutes / 60`.
- Truck type sampled from normalized mix.
- Trucks appended to `state.waiting_queue`.

## 5.2 Dock Flow Equations

For each busy dock at each step:

- `headroom = capacity - staging_occupancy`
- `inflow = min(unload_rate*dt, truck_remaining, headroom)`
- `outflow = min(clear_rate*dt, staging_occupancy + inflow)`
- `staging_next = clamp(staging + inflow - outflow, 0, capacity)`
- `truck_remaining_next = max(truck_remaining - inflow, 0)`

Unload-rate model:

- Floor trucks: workers only.
- Palletized trucks: forklifts only.

Clear-rate model:

- Floor load in staging: workers only.
- Palletized load in staging: forklifts only.

## 5.3 Strict Dock Release Rule

A dock is considered free only when:

- `current_truck is None`
- `staging_occupancy_units <= EPSILON`

No new truck dispatch occurs until both are true.

## 5.4 Dispatch and Resource Seeding

- Trucks dispatch from queue to docks that pass `can_accept_next_truck`.
- If newly assigned truck has zero effective unload rate, system tries minimal feasible assignment from idle resources so unloading can progress.

## 5.5 Trigger Detection

- `dock_freed`: emitted when a dock transitions to fully free.
- `staging_threshold`: emitted only on low->high crossing (hysteresis).
- `review_timer`: emitted at configured review cadence.

Hysteresis behavior:

- Fire when occupancy ratio >= `threshold_high` and alert inactive.
- Reset alert only when ratio <= `threshold_low`.

---

## 6. Recommendation Logic

Implemented in `yard/recommendation.py`.

## 6.1 Candidate Actions

Candidate set (deduplicated):

1. `keep_current_plan`
2. `shift_one_worker_to_most_loaded_dock`
3. `shift_one_forklift_to_most_loaded_dock`
4. `prioritize_clearing_at_riskiest_dock` (may set gate hold)

Candidate generation respects:

- Non-negative assignments.
- Per-dock max unloaders.
- Global worker/forklift limits.
- Active dock set only.

## 6.2 Evaluation

For each candidate:

1. Deep-copy current `YardState`.
2. Apply candidate assignments.
3. Simulate lookahead horizon.
4. Extract predicted metrics.
5. Compute weighted score (lower is better):
   - wait + time-in-system + queue + strong staging-risk penalty.

## 6.3 Selection and Churn Guard

- Lowest-score action is provisional best.
- If `keep_current_plan` is close enough (relative improvement below `min_score_improvement_to_switch`), keep current plan to reduce plan flapping.
- Output includes plain-language rationale and all candidate scores.

---

## 7. Engine Orchestration

Implemented in `yard/engine.py`.

Main responsibilities:

- Initialize clean startup state (`initialize_state`).
- Update supervisor controls without resetting existing operations (`update_supervisor_inputs`).
- Validate and apply actions with hard constraints (`apply_action`).
- Gate recommendation generation on trigger existence only (`recommend_on_triggers`).
- Run full minute loop (`run_minute_cycle`).
- Recompute near-term KPI cache (`refresh_kpi_cache`).
- Convert live state to dashboard snapshot contract (`snapshot_from_state`).

Important behavior:

- Recommendations are **not auto-applied** in engine/runtime APIs.
- Inactive docks are kept at zero assignments.
- Active dock reductions do not deactivate docks that are still processing load.

---

## 8. Dashboard Runtime and UI Contract

Implemented in `yard/dashboard_runtime.py` and `yard/dashboard_server.py`.

## 8.1 Runtime Responsibilities

- Owns state + seeded RNG.
- Provides step/run control (`step(minutes)`).
- Records trend history (queue, arrivals, max staging occupancy %).
- Tracks recommendation decision state (`pending`, `applied`, `kept_current_plan`, `none`).
- Enforces v1 rule: idle/inactive docks must have zero assignments.
- Builds full dashboard payload.

## 8.2 Dashboard Payload Shape (Top-Level)

Returned by `get_dashboard_payload()` / `/api/state`:

- `minute`
- `supervisor_inputs`
- `kpis`
- `recommendation`
- `staging_status`
- `dock_status`
- `resource_summary`
- `verification`
- `trends`

## 8.3 Verification Cards

Runtime computes two lightweight statistical/queueing cards:

- Spec 3: Little’s Law consistency check from trend-derived arrival rate and predicted wait.
- Spec 4: CI half-width ratio over queue observations.

Cards can be `pass`, `warn`, or `insufficient_data`.

## 8.4 Embedded Frontend

`dashboard_server.py` embeds a full HTML/CSS/JS page:

- Supervisor inputs form.
- Step/run controls.
- KPI cards.
- Recommendation panel with apply/keep actions.
- Staging traffic-light cards.
- Dock status table with ETA text.
- Verification cards.
- SVG trend charts.

Client JS uses `fetch` to call JSON endpoints and rerender entire view from payload.

---

## 9. File-by-File Reference

## 9.1 Root

| Path | Status | Role |
|---|---|---|
| `README.md` | Active | Quick-start and high-level v1 summary. |
| `pytest.ini` | Active | Test discovery config (`tests/`, no cache provider plugin). |
| `ise_engine.py` | Legacy reference | Older advanced ISE pipeline (Pandas/SciPy DES + MILP). Not part of active v1 runtime path. |
| `sample_output.json` | Legacy reference | Example output from legacy `run_ise_cycle` shape. |

## 9.2 Docs

| Path | Status | Role |
|---|---|---|
| `docs/v1_system_design.md` | Active reference | v1 design rationale and intended behavior. |
| `docs/phase2_implementation_plan.md` | Active reference | historical implementation plan/checklist for v1. |
| `docs/project_reference.md` | Active reference | this standalone full-system reference document. |

## 9.3 Package: `yard/`

| Path | Status | Role |
|---|---|---|
| `yard/__init__.py` | Active | Package exports for core classes/functions. |
| `yard/config.py` | Active | Tunable defaults and truck mix/load definitions. |
| `yard/models.py` | Active | Dataclasses/literals representing full system state and contracts. |
| `yard/simulation.py` | Active | Minute-step simulation, arrivals, dispatch, trigger detection, lookahead simulation. |
| `yard/recommendation.py` | Active | Candidate generation, evaluation, scoring, and best-action selection. |
| `yard/engine.py` | Active | Top-level orchestration and snapshot building. |
| `yard/dashboard_runtime.py` | Active | Runtime wrapper for dashboard interactions and payload shaping. |
| `yard/dashboard_server.py` | Active | HTTP server + embedded single-page dashboard UI. |
| `yard/demo_runner.py` | Active | CLI demo loop for observing system behavior. |

## 9.4 Tests: `tests/`

| Path | Role |
|---|---|
| `tests/__init__.py` | Package marker. |
| `tests/conftest.py` | Adds repo root to import path for tests. |
| `tests/fixtures_dashboard.py` | Reusable seeded runtime/HTTP fixtures, assertion helpers, API helper calls. |
| `tests/test_models.py` | Dataclass-level behavior checks (dock acceptance, idle resource counts). |
| `tests/test_simulation.py` | Arrival statistics, deterministic seeding, flow equations, strict release constraints. |
| `tests/test_triggers.py` | Threshold hysteresis and review-timer cadence validation. |
| `tests/test_recommendation.py` | Candidate feasibility + selection/switch-guard behavior. |
| `tests/test_engine.py` | Engine integration checks for action effects and recommendation consistency. |
| `tests/test_logic_validation.py` | Cross-cutting invariants (release timing, no freezes, KPI refresh, runtime consistency). |
| `tests/test_dashboard_runtime.py` | Runtime payload, recommendation lifecycle, supervisor updates, verification cards. |
| `tests/test_dashboard_server.py` | Server API smoke flow and static HTML content checks. |
| `tests/test_dashboard_api_contract.py` | HTTP contract lifecycle and error behavior checks. |
| `tests/test_dashboard_scenarios.py` | Rich runtime scenario suite (no HTTP layer). |
| `tests/test_dashboard_api_scenarios.py` | Same scenario style via full HTTP API path. |
| `tests/test_end_to_end_scenarios.py` | End-to-end scenario validation including apply/keep branching effects. |

Note: temporary directories like `pytest-cache-files-*` may exist locally but are not part of the documented source architecture.

---

## 10. Important Invariants and Design Decisions

1. **Strict release condition**: truck can only be replaced when truck load is fully unloaded and staging is fully cleared.
2. **Trigger-gated recommendation**: recommendation evaluation runs only when trigger events exist.
3. **No forced auto-execution**: recommendation generation and recommendation application are separate decisions.
4. **Threshold hysteresis**: avoids repeated threshold-trigger spam when occupancy remains high.
5. **Plan churn guard**: recommendation switch requires minimum relative score improvement.
6. **Idle dock assignment rule (dashboard runtime)**: idle/inactive docks must hold zero assigned workers/forklifts.
7. **Safe active-dock reduction**: dock deactivation skips docks with in-progress work to avoid freezing active operations.
8. **State-first architecture**: all logic mutates/reads shared `YardState`; snapshots are derived views.

---

## 11. Dependencies

## 11.1 Active v1 Runtime

Only Python standard library modules are required for active `yard/` runtime and dashboard server.

## 11.2 Testing

- `pytest` required for test suite.

## 11.3 Legacy ISE Path

`ise_engine.py` requires additional scientific stack:

- `numpy`
- `pandas`
- `scipy`

These are not required for the active v1 `yard/` flow unless legacy code is intentionally used.

---

## 12. Legacy `ise_engine.py` (Reference Only)

Legacy engine provides a richer but separate pipeline:

1. Build statistical snapshot from structured inputs (`build_snapshot_from_inputs`).
2. Forecast rates with smoothing and low/baseline/high scenarios (`make_forecast`).
3. Solve feasible worker/forklift assignment with MILP (`solve_feasible_allocation`).
4. Evaluate actions via discrete-event simulation (`simulate_action`) over replications.
5. Compute verification metrics (Little’s Law, CI half-width ratio).
6. Return ranked evaluation bundle (`run_ise_cycle`).

Artifacts:

- `sample_output.json` matches this legacy output structure (`snapshot`, `forecast`, `best_recommendation`, `verification`, `evaluations`).

---

## 13. Practical Change Guide

If you modify logic, update both implementation and corresponding tests:

- Change physics/flows -> update `test_simulation.py`, `test_logic_validation.py`.
- Change triggers -> update `test_triggers.py` and scenario tests.
- Change recommendation policy -> update `test_recommendation.py` + scenario assertions.
- Change payload/API schema -> update runtime/server contract tests and scenario suites.

Safest extension order:

1. Update `models.py`/`config.py` contracts.
2. Update simulation or recommendation logic.
3. Update `engine.py` integration.
4. Update runtime payload mapping.
5. Update/add tests.

---

## 14. Quick Start (Operational)

- Demo run: `python -m yard.demo_runner`
- Dashboard server: `python -m yard.dashboard_server` then open `http://127.0.0.1:8787`
- Tests: `pytest -q`

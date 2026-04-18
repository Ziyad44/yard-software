# Smart Truck Yard & Dock Queue Management - Version 1 Design

## 1) Feasibility and Design Intent

This version is feasible as a small, testable Python backend with:
- a 1-minute discrete simulation loop,
- simple event triggers for re-evaluation,
- a small candidate-action recommender,
- explicit state updates when a recommendation is applied.

The system is decision support for a dock supervisor, not autopilot.

## 2) Requirement Consistency Check and Final Fixes

The requirements are internally consistent. The only practical stabilization fix needed is to avoid repeated trigger spam when staging remains high. Version 1 resolves this with hysteresis:
- trigger when occupancy ratio crosses above `high_threshold`,
- clear alert only after it drops below `low_threshold`.

No other requirement conflicts were found.

## 3) Final Version-1 Operational Flow

1. Initialize live state from supervisor inputs:
   - available workers/forklifts,
   - active docks,
   - max unloaders per dock,
   - simple constraints.
2. Receive gate arrival events (camera or simulator) and append trucks to queue.
3. Run simulation in 1-minute steps.
4. For each active dock, update unloading + staging + clearing:
   - unload from truck into staging,
   - clear from staging into remove/clearing zone.
5. Enforce strict dock-release rule:
   - dock is available for next truck only when `truck_remaining_load == 0` AND `staging_occupancy == 0`.
6. Detect triggers (dock freed, staging threshold, review timer).
7. Only when triggered, evaluate candidate actions by short lookahead simulation.
8. Recommend one action in plain language.
9. If supervisor applies recommendation:
   - update live assignments/state immediately,
   - all future simulation/recommendation cycles use updated live state.

## 4) Physical Simulation Model (Minimal, Explicit)

### Time model
- Fixed step `dt = 1 minute`.

### Fixed dock/staging structure
- Each dock has exactly one staging area.
- Each dock has one remove/clearing zone (modeled as outflow rate).
- Staging capacity is fixed at `100` units for every dock.

### Truck and staging flow equations per dock per step

For each busy dock:
- `headroom = 100 - staging_occupancy`
- `inflow = min(unload_rate * dt, truck_remaining_load, max(headroom, 0))`
- `outflow = min(clear_rate * dt, staging_occupancy + inflow)`
- `staging_occupancy_next = clamp(staging_occupancy + inflow - outflow, 0, 100)`
- `truck_remaining_load_next = max(truck_remaining_load - inflow, 0)`

Behavioral rules:
- If staging is full (`headroom == 0`), unloading pauses automatically (`inflow = 0`).
- Clearing can continue while unloading is paused.
- Dock frees only when truck load is fully unloaded and staging is fully cleared.

## 5) Resource Logic (Simple and Explainable)

### Truck types
Six explicit combinations:
- `small_floor`, `medium_floor`, `large_floor`
- `small_palletized`, `medium_palletized`, `large_palletized`

### Resource influence
Version 1 uses linear rates:
- floor-loaded unloading depends mainly on workers,
- palletized unloading depends mainly on forklifts,
- staging clearing uses workers and forklifts.

Default rate form:
- `floor_unload_rate = a_w * workers + a_f * forklifts`
- `pallet_unload_rate = b_f * forklifts + b_w * workers`
- `clear_rate = c_w * workers + c_f * forklifts`

With coefficients configured in `config.py` so they remain easy to tune.

### Assignment consistency
- Global assignment limits must always hold:
  - total assigned workers `<= available_workers`
  - total assigned forklifts `<= available_forklifts`
- Per-dock worker assignment `<= max_unloaders_per_dock`
- Recommendation apply step updates these assignments in live state.

## 6) Arrival Simulator (Version 1)

Minimal stochastic arrival generator:
- configurable average arrival rate per hour,
- Poisson arrivals per minute,
- configurable truck-type mix probabilities,
- deterministic demo mode supported via seed.

Purpose: end-to-end testing/demo, not production forecasting.

## 7) Trigger Policy (Limited and Stable)

Re-evaluate only on:
1. `dock_freed`: dock transitions to available.
2. `staging_threshold`: dock occupancy ratio crosses above threshold.
3. `review_timer`: periodic review interval reached.

Stability controls:
- staging hysteresis (`high_threshold` / `low_threshold`),
- fixed review interval (default 15 minutes),
- recommendation switch guard: keep current plan unless improvement exceeds small threshold.

## 8) Recommendation Logic (Minimal Candidate Set)

At each trigger event:
1. Read current live state.
2. Build a small candidate action set.
3. Simulate short horizon for each candidate.
4. Score outcomes.
5. Select best action and produce plain-language rationale.

Version-1 candidate actions:
- `keep_current_plan`
- `shift_one_worker_to_most_loaded_dock`
- `shift_one_forklift_to_most_loaded_dock`
- `prioritize_clearing_at_riskiest_dock` (optionally holds next gate release)

Example plain-language outputs:
- "Assign +1 worker to Dock 2"
- "Assign +1 forklift to Dock 3"
- "Prioritize clearing at Dock 1"
- "Hold next gate release"
- "Keep current plan"

## 9) Minimum State Model

Version 1 uses lightweight dataclasses:

- `Truck`
  - id, truck_type, remaining_load_units, initial_load_units
  - gate_arrival_minute, assigned_dock_id, unload_start_minute
- `StagingAreaState`
  - dock_id, occupancy_units, capacity_units (=100)
  - threshold_high, threshold_low, threshold_alert_active
- `DockState`
  - dock_id, active flag
  - current_truck (optional)
  - assigned_workers, assigned_forklifts
  - staging (one per dock)
- `ResourcePool`
  - total_workers, total_forklifts
  - helper properties for assigned/idle counts
- `SystemSnapshot`
  - time, queue length, per-dock summaries, resource summary, key KPIs
- `Action`
  - action name
  - workers_by_dock / forklifts_by_dock
  - hold_gate_release flag
  - notes
- `Recommendation`
  - selected action, score, rationale, candidate scores
- `TriggerEvent`
  - trigger type, minute, optional dock_id, reason
- `YardState`
  - global live state container:
    - current minute
    - waiting queue
    - dock map
    - resources
    - active action
    - trigger/review bookkeeping
    - KPI cache

## 10) Minimal Code Architecture

Small package only:
- `yard/config.py`: constants and tunable defaults
- `yard/models.py`: dataclasses and enums/literals
- `yard/simulation.py`: minute-step state update, arrival simulator, trigger detection helpers
- `yard/recommendation.py`: candidate generation, lookahead scoring, selection logic
- `yard/engine.py`: orchestrates cycle (ingest -> step -> trigger -> recommend/apply)
- `yard/demo_runner.py`: local end-to-end demo entry point
- `tests/`: skeleton tests for models, simulation, triggers, recommendation, engine

## 11) Version-1 Scope

In scope:
- physically consistent dock/staging flow,
- trigger-based re-evaluation,
- simple arrivals and lookahead recommendation,
- live state mutation on action apply,
- backend shape ready for dashboard integration.

Out of scope:
- full dashboard UI,
- CV model implementation,
- advanced optimization/MILP,
- distributed/microservice architecture,
- high-fidelity forecasting or digital twin complexity.

## 12) Dashboard Readiness Notes

Backend state supports future cards/panels for:
- queue length,
- predicted wait/time-in-system,
- dock utilization,
- staging risk and per-dock traffic-light state,
- current dock assignments,
- resource totals/assigned/idle,
- recommendation text,
- placeholders for verification/trend series.

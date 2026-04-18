# Phase 2 Implementation Plan

## Goal

Implement a complete, testable version-1 backend using the Phase 1 design and scaffolding.

## Step 1 - Arrival Flow

1. Finalize per-minute arrival generation in `yard/simulation.py`.
2. Add deterministic demo mode behavior (seed control).
3. Verify truck-type mix normalization and handling for edge cases.
4. Add tests:
   - arrival count behavior under low/high rates,
   - mix sanity check over repeated samples,
   - deterministic reproducibility with fixed seed.

## Step 2 - Per-Dock Physical Simulation

1. Complete minute-step flow for each dock:
   - unloading inflow into staging,
   - clearing outflow from staging,
   - strict staging-capacity blocking.
2. Enforce strict dock release condition:
   - release only when truck remaining load is zero and staging occupancy is zero.
3. Add tests:
   - conservation of flow (`occupancy_next = occupancy + inflow - outflow`),
   - unloading pause when staging full,
   - no early dock release when staging still has load.

## Step 3 - Trigger Detection and Stability

1. Complete trigger emission logic:
   - `dock_freed`,
   - `staging_threshold` crossing,
   - `review_timer`.
2. Add hysteresis behavior for staging threshold (`high` / `low`).
3. Ensure trigger frequency stays controlled (no threshold spam).
4. Add tests:
   - threshold crossing fires once above `high`,
   - no repeated fire while still above `high`,
   - alert reset only below `low`,
   - periodic review timer cadence.

## Step 4 - Recommendation Evaluation Loop

1. Implement lookahead evaluation:
   - clone live state for each candidate action,
   - simulate short horizon (default 30 minutes),
   - compute comparable metrics.
2. Finalize scoring and tie-break logic.
3. Apply switch guard:
   - keep current plan unless improvement exceeds configured minimum.
4. Add tests:
   - candidate generation feasibility constraints,
   - selection chooses lower score,
   - switch guard prevents noisy plan flipping.

## Step 5 - Live State Mutation and Engine Cycle

1. Finalize `engine.run_minute_cycle`:
   - step simulation,
   - detect triggers,
   - produce recommendation only on trigger,
   - keep recommendation decoupled from automatic execution.
2. Ensure applied actions mutate live assignments/resources.
3. Add tests:
   - applied action changes next-cycle behavior,
   - recommendation and live state remain consistent.

## Step 6 - KPI Snapshot and Dashboard Readiness

1. Expand `SystemSnapshot` generation for v1 dashboard cards:
   - queue length,
   - predicted wait/time in system,
   - utilization,
   - staging risk,
   - recommended action text.
2. Add per-dock and resource summaries required by dashboard layout.
3. Add placeholder verification fields for later ISE card integration.

## Step 7 - End-to-End Demo Validation

1. Run `yard/demo_runner.py` with a small scenario.
2. Confirm full loop behavior:
   - arrivals create queue,
   - docks process trucks with staging interaction,
   - triggers fire at expected points,
   - recommendation is produced and can be applied,
   - next cycle reflects updated assignments.

## Exit Criteria for Phase 2

- End-to-end minute-loop runs without manual intervention.
- Trigger policy enforced exactly as designed.
- Recommendation logic uses simulated outcomes and updates future live behavior after apply.
- Core tests for flow, triggers, recommendation, and engine pass.

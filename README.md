# Smart Truck Yard & Dock Queue Management

This repository now contains a version-1 smart yard backend plus a local operational dashboard.

The old `ise_engine.py` and `sample_output.json` are kept as historical reference only.  
The version-1 architecture for new implementation work is documented in:
- `docs/v1_system_design.md`
- `docs/phase2_implementation_plan.md`

## Current Deliverables

- Version-1 backend simulation/recommendation engine
- Trigger-driven recommendation cycle with apply-to-live-state behavior
- KPI snapshot generation for dashboard consumption
- Local single-page dashboard for supervisor operations
- Tests for simulation, triggers, recommendation, engine, and dashboard runtime

## Minimal Architecture

- `yard/config.py`
- `yard/models.py`
- `yard/simulation.py`
- `yard/recommendation.py`
- `yard/forecasting.py`
- `yard/evaluation.py`
- `yard/verification.py`
- `yard/engine.py`
- `yard/demo_runner.py`
- `yard/dashboard_runtime.py`
- `yard/dashboard_server.py`
- `tests/`

## Quick Start

Run demo:

```bash
python -m yard.demo_runner
```

Run dashboard:

```bash
python -m yard.dashboard_server
```

Then open `http://127.0.0.1:8787`.
Keep that terminal running while using the dashboard, and stop it with `Ctrl+C` when done.

Run tests:

```bash
pytest -q
```

## Notes

- Version 1 keeps a strict dock-release rule:
  - truck remaining load must be zero
  - staging occupancy must be zero
- Truck/load handling is exclusive by load type:
  - floor-loaded trucks use workers only (unload + clear)
  - palletized trucks use forklifts only (unload + clear)
- Re-evaluation is trigger-based only:
  - dock freed
  - staging threshold crossed
  - review timer reached
- Dashboard supports:
  - Operations page with exactly 5 sections:
    - Live Operations
    - Dock & Staging
    - Resource Panel
    - Recommendation Panel
    - Analytics
  - separate Queue & Gate History page tab,
  - supervisor input updates and manual step/run controls,
  - 2-second auto-refresh polling of live state,
  - recommendation accept/reject workflow,
  - dark/light theme toggle with dark default and persisted preference,
  - queue and gate-history tables (arrival/departure/total time in system),
  - queue/staging/utilization trend charts and ISE-style verification cards.
- ISE-inspired analytics pipeline now includes:
  - low/baseline/high arrival forecast scenarios,
  - replication-based action evaluation,
  - robust action scoring across scenarios,
  - Little's Law and CI half-width verification bundles,
  - enriched output structure: `snapshot`, `forecast`, `best_recommendation`, `verification`, `evaluations`.

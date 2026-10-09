# Contributor instructions

Read `CLAUDE.md` for research and operational invariants. This project uses
paper accounts and separates model proposals from deterministic execution.

## Ownership and layout

- `core/`: data contracts, broker adapters, accounting, risk and model clients.
- `systems/`: strategy book implementations; keep S1 deterministic.
- `runtime/`: orchestration and persistence; retain additive state migrations.
- `research/` and `backtest/`: causal evaluation, experiment records and agents.
- `serving/` and `deploy/kubernetes/`: isolated model API and GPU infrastructure.
- `dashboard/`: read-only monitoring; treat all state text as untrusted input.
- `tests/`: offline checks. Archived proposal copies are not the active suite.

Assign disjoint files to parallel contributors, agree on interfaces and review
their combined change. Keep model/research evidence separate from accounting
or market-performance evidence. Preserve unrelated local work and archives.

## Delivery checks

Install the documented dependencies and run the active offline suite. Validate
container startup and Kubernetes manifests when those components change.
Measure real model serving only against a genuine endpoint; record failures,
versions, workload, hardware verification and cleanup status together.

Use explicit deployment targets, environment-only credentials and narrow build
contexts. Do not commit runtime state, accounts, personal paths or secrets.
Describe what was tested and any remaining limitations without claiming
profitable trading, continuous uptime or hardware deployment from configuration.

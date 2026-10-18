# Build and verification log

Date: 2026-10-17

This log records the main implementation iterations and the checks used to decide whether to continue.

## Iteration 1 — end-to-end resolution baseline

- Added ticket creation, run creation, structured proposals, reviewer approval, mock refund execution, and audit events.
- Added synthetic customer/order/return/payment data and a deterministic evaluation harness.
- Checked import safety, customer ownership, proposal/approval binding, and idempotent provider operations.

## Iteration 2 — shipping evidence

- Added shipment records and a mock shipping adapter.
- Switched order-status answers to the shipment record rather than trusting the order's general status field.
- Missing, stale, or unavailable shipment data now escalates; provider failure and latency can be injected.
- Added tests for missing/stale shipment evidence and provider outages.

## Iteration 3 — ticketing sync and recovery

- Added a durable mock ticketing operation store and explicit local sync status.
- Added idempotent sync, timeout-after-accept reconciliation, audit events, and a reviewer retry endpoint.
- Added a UI retry panel for pending ticketing updates.
- Tested the timeout-after-accept path and confirmed retry reuses one provider operation.

## Iteration 4 — clarification workflow and bounded execution

- Added persisted customer messages and `POST /runs/{run_id}/messages`.
- Damaged/faulty claims take precedence over refund intent and request damage/photo evidence before specialist review.
- Added ownership validation when attaching an order, a message-history endpoint, and limits on follow-up count, workflow runs, and complaint size.
- Added tests for the clarification loop, cross-customer order attachment, and bounded follow-ups.

## Iteration 5 — version-aware policy registry

- Added Alembic migrations for the policy document/chunk tables.
- Added policy version/effective-date frontmatter, immutable body hashes, idempotent ingestion, effective-date filtering, and conflict escalation when active versions overlap.
- Missing policy evidence now escalates instead of permitting a refund based only on transaction data.
- Added tests for idempotent ingestion, version transitions, and policy conflicts.

## Iteration 6 — financial and authorization hardening

- Rechecked payment settlement and order delivery before refund eligibility.
- Bound the approval hash to customer, order, action, amount, rationale, evidence, and policy citations.
- Rechecked order ownership in investigation and again before financial execution.
- Invalidated stale/changed proposals and routed failed post-approval revalidation to review or reconciliation.
- Added mock payment failure, delay, and timeout-after-acceptance controls.

## Iteration 7 — durable node checkpoints and resume

- Persisted each completed workflow node's structured state and current step in the database.
- Stored the run before starting execution so an interrupted run remains discoverable.
- Added a reviewer-only resume endpoint and a customer-scoped latest-run lookup for the UI to recover a lost response.
- Added a test that injects a failure after investigation, verifies the last checkpoint, resumes, and confirms exactly one proposal is created.

## Iteration 8 — request observability and abuse controls

- Added allow-listed JSON request logs with request ID, method, path, status, latency, and error type; request bodies and authorization headers are never logged.
- Added validated `X-Request-ID` propagation and in-memory rate limits for write and recovery endpoints, with `429` and `Retry-After` behavior.
- Added tests for request IDs and rate limiting. The limiter is intentionally process-local; shared rate limiting is a deployment task.

## Current validation snapshot

- Automated tests: **47 passed**.
- Synthetic evaluation: **60/60 cases passed** across six declared groups.
- Held-out synthetic subset: **12/12 passed** under the documented deterministic split.
- Unauthorized actions detected: **0**.
- Duplicate refund effects detected: **0**.
- Verified resolution rate: **100% of 28 cases designated resolvable** in this synthetic suite.
- Alembic schema check: no model/migration drift on SQLite.
- Migration upgrade/downgrade and repeatable seed: tested on SQLite.
- UI JavaScript: syntax-checked with Node; Docker Compose YAML parsed.
- Live HTTP smoke: request-ID propagation, JSON request logging, latest-run recovery lookup, approved refund, ticket sync, damaged-item clarification, and message history exercised.
- Live process restart: accepted-but-timed-out refund reconciled from the persisted provider operation, and a pending ticketing update synced on retry after restarting Uvicorn against the same SQLite database.

These are local synthetic checks, not production benchmarks. The evaluation uses no live Gemini calls and does not prove PostgreSQL concurrency or independent network-service behavior.

## Remaining work requiring external environment or credentials

See [`specification-audit.md`](specification-audit.md) for the full source-to-implementation checklist. The main blockers are dependency registry access for a complete lockfile, a real Gemini/LangGraph runtime check, native LangGraph checkpoint/worker integration, Docker/PostgreSQL validation, GitHub repository creation/push, production authentication, and real provider contracts.

## Iteration 9 — evaluation entry-point robustness

- Fixed `python scripts/evaluate.py` from a fresh repository checkout: direct script execution now adds the repository root to `sys.path` before importing the application package.
- Documented both supported evaluation commands in the README.
- Validation after the change: 47 automated tests passed; both `python scripts/evaluate.py` and `python -m scripts.evaluate` completed with 60/60 synthetic cases passing; `compileall` passed.
- Environment note: Ruff, Gemini SDK, and LangGraph are not installed in this current execution environment, so linting and live model/LangGraph execution were not verified here.


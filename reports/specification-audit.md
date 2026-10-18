# Specification audit — Customer Support Resolution Agent

This audit maps the original project brief to the current repository. It distinguishes implemented behavior from partial implementations and external validation that has not been possible in this workspace.

## Implemented and locally validated

- [x] FastAPI ticket, workflow-run, proposal, audit-event, approval, and health endpoints.
- [x] Customer ownership checks on customer-scoped ticket/order/run/message endpoints; foreign order IDs are not disclosed.
- [x] Synthetic customers, orders, returns, payments, shipment records, and version-controlled Markdown policy files.
- [x] Complaint classification with optional Gemini structured output and deterministic fallback; damage/fault claims override refund intent to require evidence and specialist review.
- [x] Structured workflow state across classification, investigation, policy retrieval, and proposal generation.
- [x] Application-level state snapshots persisted after each completed node; reviewer-only resume restarts from the last committed checkpoint, and raw complaint text is excluded from checkpoint JSON.
- [x] Structured JSON request logs with request IDs/latency and no request-body/header logging; in-memory rate limits cover ticket creation, workflow starts, customer follow-ups, reviewer decisions, and recovery endpoints.
- [x] Policy citations; missing policy evidence causes escalation rather than an inferred refund.
- [x] Versioned policy metadata is ingested from Markdown frontmatter; effective-date filtering and immutable content hashes are enforced. Overlapping active versions of the same policy family trigger escalation.
- [x] Shipping adapter checks latest shipment status and escalates missing, stale, or unavailable status.
- [x] Refund eligibility enforced by ordinary code, including return status, inspection, payment record, and remaining balance.
- [x] Reviewer approval persisted and bound to a hashed proposal payload; expired approvals are invalidated and require a new run.
- [x] Mock payment provider supports deterministic pre-acceptance failure, latency, and timeout-after-acceptance injection.
- [x] Refund operation reservation committed before provider call; one active refund reservation per order; stable idempotency key; provider lookup before retry.
- [x] Timeout-after-acceptance recovery test; uncertain payment outcomes remain `pending_reconciliation` rather than being reported as success. Live Uvicorn process restarts were exercised against SQLite: an accepted-but-timed-out refund reconciled without duplication, and a pending ticketing update synced on retry.
- [x] Ticketing sync status, durable mock ticketing operation, timeout reconciliation, and reviewer-triggered retry endpoint.
- [x] Damaged-item clarification flow with persisted customer messages, ownership-checked order attachment, bounded follow-up/run counts, and a fresh workflow run after follow-up.
- [x] Alembic initial schema, ticket-message migration, versioned policy registry migration, and durable-checkpoint migration; fresh migration, downgrade/upgrade, and seed idempotency tested on SQLite.
- [x] 47 automated tests pass locally; deterministic evaluation contains 60 cases across six groups, with 60/60 passing in the latest run, zero detected unauthorized actions, and zero duplicate refund effects.
- [x] UI JavaScript syntax checked with Node; live Uvicorn smoke test exercised ticket creation, workflow, reviewer approval, ticket sync, and damaged-item clarification.

## Partially implemented — deliberate prototype boundaries

- [ ] Rate limiting is process-local and does not coordinate across workers/replicas; configure a shared limiter for deployment.
- [ ] Authentication is demo-only: customer identity is supplied as `X-Customer-ID`; reviewer authorization uses a configured static token. Replace with a proper identity provider and reviewer role checks before production.
- [ ] Payment, shipping, and ticketing adapters are in-process mock integrations backed by the same database, not separately deployed HTTP services. Failure and delay injection is supported, but true network partitions and independent service databases are not proven.
- [ ] Policy retrieval is a transparent lexical baseline over versioned PostgreSQL/SQLite policy documents and section chunks. Embeddings/pgvector retrieval and a larger semantic/conflict evaluation remain future work; active-version overlap is detected and escalated.
- [ ] Damaged-item evidence is represented by a customer-provided photo/attachment reference. The prototype does not upload or inspect actual images and does not automatically authorize replacements.
- [ ] LangGraph orchestrates the investigation path when installed. Application-level snapshots and a reviewer resume endpoint exist, but the native LangGraph checkpointer, automated stale-run detection, and a background job scheduler remain unimplemented.
- [ ] The evaluation held-out split is a deterministic synthetic reporting split, not a production or independently collected benchmark. Model calls and model cost are intentionally zero in this rules-only evaluation.
- [ ] The original brief's larger 60-case categories are represented, but live PostgreSQL concurrency stress, worker-crash recovery outside the refund path, and real provider contracts remain unverified.

## External actions / environment-dependent validation

- [ ] Install project dependencies from a network-enabled environment and test Gemini with a valid `GEMINI_API_KEY`; the current environment does not have `google-genai` or LangGraph installed.
- [ ] Generate and commit `uv.lock` (or another complete transitive lockfile). `uv lock` was attempted but could not reach PyPI because network access was unavailable; no incomplete lockfile was committed.
- [ ] Run `docker compose up --build` and verify PostgreSQL-backed migrations, seeded UI, and all scenarios. Docker is not installed in this workspace.
- [ ] Run the refund reservation and reconciliation suite against real PostgreSQL with concurrent requests and process restarts; the local process-restart check used SQLite.
- [ ] Create the GitHub repository and push this source. The available GitHub integration in this workspace does not expose repository creation; README contains the `gh repo create` command.
- [ ] Configure a real deployment URL, secret management, and production authentication only after the local and PostgreSQL checks pass.

## Acceptance rule

Do not describe partial or external items as complete. Keep the synthetic metrics labelled as development checks and do not connect real payment credentials or customer data to this prototype.

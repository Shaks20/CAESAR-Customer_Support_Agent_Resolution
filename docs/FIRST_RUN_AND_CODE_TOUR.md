# First Run and Code Tour

This guide is for running the MVP locally and learning it one layer at a time. You do not need a Gemini API key or Docker for the first run.

## 1. What you are running

CAESAR is a support-workflow prototype for a fictional electronics retailer. It uses synthetic customers/orders and mock shipping, ticketing, and payment providers.

A customer complaint goes through these stages:

1. **Receive the complaint** — FastAPI creates a ticket and a workflow run.
2. **Classify** — the classifier labels the complaint (for example, refund or order status). Gemini is optional; rules-based classification is the fallback.
3. **Investigate** — the workflow checks the customer's order and relevant transaction/shipment records.
4. **Retrieve policy** — versioned policy documents are searched and supporting sections are attached as citations.
5. **Decide what can be proposed** — deterministic application code checks eligibility. The model does not decide whether a refund is allowed.
6. **Pause for a reviewer** — a refund proposal is not a refund. A reviewer must approve the exact proposal.
7. **Revalidate and execute** — before execution, the app rechecks eligibility and proposal binding, then calls the idempotent mock payment provider.
8. **Record and reconcile** — audit events are persisted. An uncertain provider outcome is treated as pending reconciliation, not reported as success.

Damaged-item complaints use a clarification/evidence path and can be escalated for specialist review. Order-status answers use the shipping adapter rather than guessing from a general order status.

## 2. Run locally on Windows (PowerShell)

Use Python 3.11 or newer. Open PowerShell in the folder where you want the project.

### Clone the repository

```powershell
git clone https://github.com/Shaks20/CAESAR-Customer_Support_Agent_Resolution.git
cd CAESAR-Customer_Support_Agent_Resolution
```

If you already cloned it earlier, enter that folder and run `git pull` instead of cloning again.

### Create and activate a virtual environment

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
```

If `py -3.11` is unavailable, install Python 3.11+ and enable the Python Launcher. If PowerShell blocks activation, use this command for the current terminal only, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### Install the project

The dependencies are declared in `pyproject.toml` (there is no need to install from a `requirements.txt` file).

```powershell
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### Create local settings, migrate the database, and seed demo data

```powershell
Copy-Item .env.example .env
python -m alembic upgrade head
python -m app.db.seed
```

The default database is a local SQLite file named `support_agent.db`. The commands above create the schema and insert synthetic demo records. Keep `.env` local; do not commit secrets.

### Start the API and UI

```powershell
python -m uvicorn app.main:app --reload
```

Open these pages in your browser:

- UI: http://127.0.0.1:8000/
- Interactive API documentation: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

Leave the terminal running while using the app. Stop the server with `Ctrl+C`.

## 3. First things to try

Use the UI first, then repeat requests through `/docs` so you can see the API boundary.

- **Eligible refund:** customer `cust_ada`, order `ord_1001`. The workflow should create a proposal and wait for reviewer approval. It must not refund automatically.
- **Ineligible refund:** customer `cust_ada`, order `ord_1002`. Its return inspection failed, so the workflow should not approve a refund.
- **Order status:** customer `cust_grace`, order `ord_2001`. The response should use the mock shipping record and tracking number.
- **Damaged item:** submit a damaged/faulty-item complaint. The workflow should ask for evidence and route the case for specialist review rather than automatically refunding.

Demo identities and reviewer tokens are for local demonstration only. Do not connect real customer information or payment credentials.

## 4. Code tour: where to look first

Read these files in this order. Avoid starting with the entire API file or every database model at once.

| File | What it is responsible for |
|---|---|
| `app/main.py` | FastAPI entry point, HTTP endpoints, customer/reviewer checks, and wiring the workflow to the API |
| `app/schemas/api.py` | Pydantic request/response shapes: what the API accepts and returns |
| `app/agents/classifier.py` | Complaint classification; optional Gemini call and deterministic fallback |
| `app/agents/workflow.py` | Workflow state, ordered nodes, checkpoint saves, resume behavior |
| `app/agents/workflow_rules.py` | Small deterministic routing/business-rule helpers |
| `app/services/policies.py` | Policy ingestion, version/effective-date handling, retrieval, and citations |
| `app/services/resolution.py` | Refund eligibility, proposal creation, approval-bound execution and revalidation |
| `app/services/payment_provider.py` | Mock payment provider, idempotency keys, and provider-operation reconciliation |
| `app/services/shipping_provider.py` | Mock source of shipment status |
| `app/services/ticketing_provider.py` and `ticketing_sync.py` | Mock ticketing operations and retry/reconciliation |
| `app/db/models.py` | Database tables and relationships |
| `app/db/seed.py` | Synthetic customers, orders, returns, payments, shipments, and policy seed call |
| `app/static/index.html` | Browser UI served at `/` |
| `policies/` | Versioned policy documents used by retrieval |
| `migrations/versions/` | Alembic schema migrations |
| `tests/` | Automated checks for rules, permissions, recovery, and edge cases |
| `scripts/evaluate.py` | Deterministic synthetic evaluation runner |

### The main workflow in one picture

```text
Customer UI
   |
   v
FastAPI endpoint (app/main.py)
   |  validates demo customer identity / creates ticket + run
   v
Workflow (app/agents/workflow.py)
   |--> classify complaint (app/agents/classifier.py)
   |--> investigate order/payment/shipment facts
   |--> retrieve versioned policy + citations (app/services/policies.py)
   |--> create proposal or escalate (app/services/resolution.py)
   v
Human reviewer approval (refunds only)
   |
   v
Re-check eligibility + proposal hash
   |
   v
Mock payment provider (idempotency + reconciliation)
   |
   v
Persist status and audit events in the database
```

The database stores business records, tickets, workflow runs/checkpoints, proposals, approvals, provider operations, messages, and audit events. The model can help classify text, but ordinary application code owns authorization and refund eligibility.

## 5. Run the checks

Open a second PowerShell terminal in the repository folder, activate the virtual environment, then run:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
python -m scripts.evaluate
```

The repository reports a baseline of 47 automated tests and 60 deterministic synthetic evaluation cases. Re-run these locally and treat the output you see as the current result. These tests do not prove production reliability, real Gemini behavior, or PostgreSQL concurrency.

## 6. Optional: use Docker and PostgreSQL later

After the SQLite flow makes sense, install Docker Desktop and start the Compose stack:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Compose starts PostgreSQL, applies migrations, seeds data, and starts the API. The database configuration is different from local SQLite. Stop it with `Ctrl+C`; use `docker compose down` to stop containers. Do not add `-v` unless you intentionally want to delete the persisted database volume.

## 7. Troubleshooting

- **`No module named ...`:** make sure `.venv` is activated and run `python -m pip install -e ".[dev]"`.
- **`alembic` or `uvicorn` is not recognized:** use `python -m alembic ...` and `python -m uvicorn ...` as shown above.
- **Port 8000 is busy:** stop the other server or run `python -m uvicorn app.main:app --reload --port 8001`; then open port 8001 in the browser.
- **Database/table errors:** stop the app, confirm `.env` has `DATABASE_URL=sqlite:///./support_agent.db`, then rerun `python -m alembic upgrade head` and `python -m app.db.seed`.
- **No Gemini API key:** expected for the first run. The rules fallback should still allow the demo workflow to run.
- **Changes are not on GitHub:** check `git status`, commit only intended files, then `git push`.

## 8. Suggested learning loop

For each feature, follow one request end-to-end: inspect the UI action, find the endpoint in `app/main.py`, follow the called function into the workflow/service, inspect the database model it reads or writes, and then find the test that protects the behavior. Make one small change, run the closest test first, then run the full suite before committing.

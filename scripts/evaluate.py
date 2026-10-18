"""Evaluate 60 synthetic business outcomes, safety invariants, and recovery paths.

Every fifth case is pre-labelled held-out for reporting. This deterministic dataset uses
no external model calls and is not evidence of production performance.
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
import sys

# Support both `python -m scripts.evaluate` and direct execution from a checkout.
# Python otherwise puts `scripts/`, rather than the repository root, on sys.path.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.workflow import start_resolution
from app.core.config import settings
from app.db.database import Base
from app.db.models import (
    ActionProposal, Approval, Customer, MockProviderOperation, MockTicketingOperation, Order, Payment,
    RefundOperation, ReturnRecord, Shipment, Ticket, WorkflowRun,
)
import app.services.resolution as resolution_service
from app.services.resolution import execute_approved_refund
from app.services.ticketing_sync import sync_ticket_status
from app.services.policies import ingest_policy_documents

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "reports" / "evaluation-baseline.json"
REVIEWER_ID = "evaluation-reviewer"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_case_records(db, customer: Customer, index: int, complaint: str, *,
                        order_status: str = "delivered", return_status: str | None = "received",
                        inspection: str | None = "passed", refunded: int | str = 0,
                        include_order: bool = True, include_return: bool = True,
                        include_payment: bool = True, include_shipment: bool = False) -> tuple[Order | None, Ticket]:
    order = None
    if include_order:
        order = Order(
            id=f"eval_order_{index}", customer_id=customer.id, product_name="Test Device",
            status=order_status, currency="INR", paid_amount_minor=10000 + index * 100,
            delivered_at=utcnow() - timedelta(days=5) if order_status == "delivered" else None,
        )
        db.add(order)
        db.flush()
        if include_return and return_status is not None and inspection is not None:
            db.add(ReturnRecord(id=f"eval_return_{index}", order_id=order.id,
                                status=return_status, inspection_status=inspection))
        if include_payment:
            refunded_minor = order.paid_amount_minor if refunded == "full" else int(refunded)
            db.add(Payment(id=f"eval_payment_{index}", order_id=order.id, status="paid",
                           refunded_amount_minor=refunded_minor))
        if include_shipment:
            db.add(Shipment(id=f"eval_shipment_{index}", order_id=order.id, status=order_status,
                            tracking_number=f"EVAL-{index:04d}", updated_at=utcnow()))
    ticket = Ticket(
        id=f"eval_ticket_{index}", customer_id=customer.id,
        order_id=order.id if order else None, complaint=complaint, status="open",
    )
    db.add(ticket)
    db.commit()
    return order, ticket


def run_case(db, customer: Customer, ticket: Ticket) -> tuple[WorkflowRun, ActionProposal]:
    run = start_resolution(db, ticket, customer)
    proposal = db.get(ActionProposal, run.proposal_id)
    return run, proposal


def approve(db, proposal: ActionProposal) -> Approval:
    approval = db.query(Approval).filter_by(proposal_id=proposal.id).one()
    approval.decision = "approved"
    approval.reviewer_id = REVIEWER_ID
    approval.decided_at = utcnow()
    db.commit()
    return approval


def row_result(name: str, category: str, expected_action: str, actual_action: str,
               passed: bool, started: float, **extra: Any) -> dict[str, Any]:
    return {
        "case": name,
        "category": category,
        "split": "held_out" if int(name.split("-")[-1]) % 5 == 0 else "development",
        "expected_action": expected_action,
        "actual_action": actual_action,
        "action_correct": expected_action == actual_action,
        "passed": bool(passed),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        **extra,
    }


def main() -> None:
    settings.gemini_api_key = ""  # Reproducible, rules-only evaluation.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = Session()
    customer = Customer(id="eval_customer", name="Evaluation Customer", email="evaluation@example.test")
    db.add(customer)
    db.commit()
    ingest_policy_documents(db)
    results: list[dict[str, Any]] = []
    index = 1

    # 1. Ten order-status and shipment questions.
    statuses = ["shipped", "processing", "delivered", "shipped", "delivered",
                "processing", "shipped", "delivered", "processing", "shipped"]
    for n, order_status in enumerate(statuses, 1):
        started = time.perf_counter()
        order, ticket = create_case_records(
            db, customer, index,
            f"Where is my order? Please share tracking and delivery status, case {n}.",
            order_status=order_status, include_shipment=True,
        )
        run, proposal = run_case(db, customer, ticket)
        evidence = json.loads(proposal.evidence_json)
        verified = (run.status == "resolved" and proposal.action_type == "inform" and any(
            item.get("fact") == "status" and item.get("value") == order_status for item in evidence
        ))
        results.append(row_result(f"order-status-{n:02d}", "order_status", "inform", proposal.action_type,
                                  verified, started, verified_outcome=verified,
                                  policy_citation_found=bool(json.loads(proposal.policy_citations_json)),
                                  human_assisted=False))
        index += 1

    # 2. Ten eligible refunds. Every action requires simulated reviewer approval.
    for n in range(1, 11):
        started = time.perf_counter()
        order, ticket = create_case_records(db, customer, index,
            f"I returned this item and need a refund, eligible scenario {n}.")
        run, proposal = run_case(db, customer, ticket)
        approval = approve(db, proposal)
        outcome = execute_approved_refund(db, proposal, approval, REVIEWER_ID)
        db.commit()
        replay = execute_approved_refund(db, proposal, approval, REVIEWER_ID)
        run.status = "resolved"
        db.commit()
        sync_ticket_status(db, ticket, run, actor=REVIEWER_ID)
        db.commit()
        db.refresh(order)
        provider_count = db.query(MockProviderOperation).filter_by(idempotency_key=f"refund:{proposal.id}").count()
        verified = (proposal.action_type == "refund" and outcome["status"] == "succeeded"
                    and order.payment.refunded_amount_minor == order.paid_amount_minor
                    and replay.get("idempotent_replay") is True and provider_count == 1)
        results.append(row_result(f"eligible-refund-{n:02d}", "eligible_refund", "refund", proposal.action_type,
                                  verified, started, verified_outcome=verified,
                                  policy_citation_found=bool(json.loads(proposal.policy_citations_json)),
                                  human_assisted=True, provider_records=provider_count,
                                  duplicate_replay_safe=replay.get("idempotent_replay") is True))
        index += 1

    # 3. Ten ineligible refunds. They must be escalated with no payment effect.
    ineligible = [
        {"return_status": "requested", "inspection": "passed"},
        {"return_status": "initiated", "inspection": "passed"},
        {"return_status": "received", "inspection": "failed"},
        {"return_status": "received", "inspection": "pending"},
        {"return_status": "received", "inspection": "not_started"},
        {"return_status": "rejected", "inspection": "passed"},
        {"return_status": "in_transit", "inspection": "passed"},
        {"return_status": None, "inspection": None, "include_return": False},
        {"return_status": "received", "inspection": "passed", "include_payment": False},
        {"return_status": "received", "inspection": "passed", "refunded": "full"},
    ]
    for n, options in enumerate(ineligible, 1):
        started = time.perf_counter()
        order, ticket = create_case_records(db, customer, index,
            f"Please refund my returned item, ineligible scenario {n}.", **options)
        run, proposal = run_case(db, customer, ticket)
        operations = db.query(RefundOperation).filter_by(order_id=order.id).count() if order else 0
        verified = proposal.action_type == "escalate" and run.status == "needs_review" and operations == 0
        results.append(row_result(f"ineligible-refund-{n:02d}", "ineligible_refund", "escalate", proposal.action_type,
                                  verified, started, verified_outcome=verified,
                                  policy_citation_found=bool(json.loads(proposal.policy_citations_json)),
                                  human_assisted=False, refund_operations=operations))
        index += 1

    # 4. Ten missing-information and ambiguous complaints.
    for n in range(1, 11):
        started = time.perf_counter()
        complaint = (f"I need a refund but cannot identify my order, case {n}." if n <= 5
                     else f"I need help with an unclear store issue, case {n}.")
        _, ticket = create_case_records(db, customer, index, complaint, include_order=n > 5)
        run, proposal = run_case(db, customer, ticket)
        verified = proposal.action_type == "escalate" and proposal.amount_minor == 0
        results.append(row_result(f"missing-ambiguous-{n:02d}", "missing_ambiguous", "escalate", proposal.action_type,
                                  verified, started, verified_outcome=verified,
                                  policy_citation_found=bool(json.loads(proposal.policy_citations_json)),
                                  human_assisted=False))
        index += 1

    # 5. Ten integration failures: provider timeouts, pre-acceptance errors, rollback recovery,
    # and ticketing timeout-after-acceptance. Every retry must reuse the stable idempotency key.
    original_create = resolution_service.create_refund
    import app.services.ticketing_sync as ticketing_sync_module
    original_ticket_update = ticketing_sync_module.update_ticket_status
    try:
        for n in range(1, 11):
            started = time.perf_counter()
            order, ticket = create_case_records(db, customer, index,
                f"I returned my item and need a refund after an integration failure, case {n}.")

            if n >= 9:
                def ticket_accept_then_timeout(ticket_id, status, idempotency_key, *, bind=None, **kwargs):
                    return original_ticket_update(ticket_id, status, idempotency_key,
                                                  bind=bind, timeout_after_accept=True)
                ticketing_sync_module.update_ticket_status = ticket_accept_then_timeout
            run, proposal = run_case(db, customer, ticket)
            if n >= 9:
                ticketing_sync_module.update_ticket_status = original_ticket_update
                db.refresh(ticket)
                initial_sync_status = ticket.ticketing_sync_status
                sync_result = sync_ticket_status(db, ticket, run, actor=REVIEWER_ID)
                db.commit()
                provider_count = db.query(MockTicketingOperation).filter_by(ticket_id=ticket.id).count()
                recovered = initial_sync_status == "pending" and sync_result["status"] == "synced" and provider_count == 1
                results.append(row_result(f"timeout-recovery-{n:02d}", "api_failure_recovery", "refund", proposal.action_type,
                                          recovered, started, verified_outcome=False, resolvable=False,
                                          recovery_verified=recovered, recovery_type="ticketing_timeout_after_accept",
                                          ticketing_sync_status=sync_result["status"], provider_records=provider_count,
                                          policy_citation_found=bool(json.loads(proposal.policy_citations_json)),
                                          human_assisted=False))
                index += 1
                continue

            approval = approve(db, proposal)
            if n <= 4:
                def accept_then_timeout(order_id, amount_minor, idempotency_key, *, bind=None):
                    original_create(order_id, amount_minor, idempotency_key, bind=bind)
                    raise TimeoutError("evaluation: provider accepted, response lost")
                resolution_service.create_refund = accept_then_timeout
                first = execute_approved_refund(db, proposal, approval, REVIEWER_ID)
                resolution_service.create_refund = original_create
                recovery_type = "provider_timeout_after_accept"
            elif n <= 6:
                def fail_before_accept(order_id, amount_minor, idempotency_key, *, bind=None):
                    raise ConnectionError("evaluation: provider unavailable before acceptance")
                resolution_service.create_refund = fail_before_accept
                first = execute_approved_refund(db, proposal, approval, REVIEWER_ID)
                resolution_service.create_refund = original_create
                recovery_type = "provider_unavailable_before_accept"
            else:
                first = execute_approved_refund(db, proposal, approval, REVIEWER_ID)
                db.rollback()  # Simulate app crash/rollback after provider acceptance.
                db.expire_all()
                proposal = db.get(ActionProposal, proposal.id)
                approval = db.get(Approval, approval.id)
                recovery_type = "application_transaction_rollback"

            db.expire_all()
            proposal = db.get(ActionProposal, proposal.id)
            approval = db.get(Approval, approval.id)
            second = execute_approved_refund(db, proposal, approval, REVIEWER_ID)
            run = db.get(WorkflowRun, run.id)
            ticket = db.get(Ticket, run.ticket_id)
            run.status = "resolved" if second["status"] == "succeeded" else "pending_reconciliation"
            db.commit()
            sync_ticket_status(db, ticket, run, actor=REVIEWER_ID)
            db.commit()
            provider_count = db.query(MockProviderOperation).filter_by(idempotency_key=f"refund:{proposal.id}").count()
            recovered = (first["status"] in {"pending_reconciliation", "succeeded"}
                         and second["status"] == "succeeded" and provider_count == 1
                         and db.get(Order, order.id).payment.refunded_amount_minor == order.paid_amount_minor)
            results.append(row_result(f"timeout-recovery-{n:02d}", "api_failure_recovery", "refund", proposal.action_type,
                                      recovered, started, verified_outcome=recovered, resolvable=True,
                                      recovery_verified=recovered, recovery_type=recovery_type,
                                      first_status=first["status"], retry_status=second["status"],
                                      provider_records=provider_count,
                                      policy_citation_found=bool(json.loads(proposal.policy_citations_json)),
                                      human_assisted=True))
            index += 1
    finally:
        resolution_service.create_refund = original_create
        ticketing_sync_module.update_ticket_status = original_ticket_update

    # 6a. Five tampered proposals must fail before any provider side effect.
    for n in range(1, 6):
        started = time.perf_counter()
        _, ticket = create_case_records(db, customer, index,
            f"I returned my item and need a refund, adversarial case {n}.")
        _, proposal = run_case(db, customer, ticket)
        approval = approve(db, proposal)
        proposal_id = proposal.id
        proposal.amount_minor += 1
        try:
            execute_approved_refund(db, proposal, approval, REVIEWER_ID)
            rejected = False
        except ValueError:
            rejected = True
            db.rollback()
        provider_count = db.query(MockProviderOperation).filter_by(idempotency_key=f"refund:{proposal_id}").count()
        verified = rejected and provider_count == 0
        results.append(row_result(f"tampered-proposal-{n:02d}", "authorization_adversarial", "refund", "refund",
                                  verified, started, verified_outcome=verified,
                                  unauthorized_action_blocked=rejected, provider_records=provider_count,
                                  human_assisted=True))
        index += 1

    # 6b. Five competing requests must not reserve or execute a second refund on an order.
    for n in range(1, 6):
        started = time.perf_counter()
        order, ticket1 = create_case_records(db, customer, index,
            f"I returned my item and need a refund, reservation owner {n}.")
        _, proposal1 = run_case(db, customer, ticket1)
        db.add(RefundOperation(id=str(uuid4()), order_id=order.id, proposal_id=proposal1.id,
                               amount_minor=proposal1.amount_minor, idempotency_key=f"refund:{proposal1.id}", status="pending"))
        db.commit()
        ticket2 = Ticket(id=f"eval_ticket_{index}_second", customer_id=customer.id, order_id=order.id,
                         complaint=f"I returned my item and need a refund, competing request {n}.", status="open")
        db.add(ticket2); db.commit()
        _, proposal2 = run_case(db, customer, ticket2)
        approval2 = approve(db, proposal2)
        try:
            execute_approved_refund(db, proposal2, approval2, REVIEWER_ID)
            blocked = False
        except ValueError:
            blocked = True
            db.rollback()
        provider_count = db.query(MockProviderOperation).filter_by(idempotency_key=f"refund:{proposal2.id}").count()
        verified = blocked and provider_count == 0
        results.append(row_result(f"competing-reservation-{n:02d}", "authorization_adversarial", "refund", proposal2.action_type,
                                  verified, started, verified_outcome=verified,
                                  unauthorized_action_blocked=blocked, provider_records=provider_count,
                                  human_assisted=True))
        index += 1

    categories = Counter(row["category"] for row in results)
    splits = Counter(row["split"] for row in results)
    held_out = [row for row in results if row["split"] == "held_out"]
    provider_rows = db.query(MockProviderOperation).all()
    ticketing_rows = db.query(MockTicketingOperation).all()
    ticketing_pending = db.query(Ticket).filter(Ticket.ticketing_sync_status != "synced").count()
    unauthorized = 0
    for provider_op in provider_rows:
        proposal_id = provider_op.idempotency_key.removeprefix("refund:")
        proposal = db.get(ActionProposal, proposal_id)
        approval = db.query(Approval).filter_by(proposal_id=proposal_id).one_or_none()
        if proposal is None or approval is None or approval.decision != "approved":
            unauthorized += 1

    amount_by_order: dict[str, int] = defaultdict(int)
    for provider_op in provider_rows:
        amount_by_order[provider_op.order_id] += provider_op.amount_minor
    duplicate_effects = sum(
        1 for order_id, total in amount_by_order.items()
        if (db.get(Order, order_id) is not None and total > db.get(Order, order_id).paid_amount_minor)
    )
    duplicate_effects += sum(
        1 for _, count in db.query(RefundOperation.proposal_id, func.count()).group_by(RefundOperation.proposal_id)
        if count > 1
    )

    passed = sum(1 for row in results if row["passed"])
    action_correct = sum(1 for row in results if row["action_correct"])
    resolvable = [row for row in results if row.get("resolvable", row["category"] in {"order_status", "eligible_refund", "api_failure_recovery"})]
    verified = sum(1 for row in resolvable if row.get("verified_outcome"))
    policy_required = [row for row in results if row["category"] in {"eligible_refund", "ineligible_refund", "api_failure_recovery"}]
    policy_hits = sum(1 for row in policy_required if row.get("policy_citation_found"))
    report = {
        "dataset": "synthetic-v1",
        "cases_total": len(results),
        "cases_passed": passed,
        "overall_correctness": round(passed / len(results), 3),
        "action_accuracy": round(action_correct / len(results), 3),
        "verified_resolution_rate": round(verified / len(resolvable), 3),
        "verified_resolution_cases": len(resolvable),
        "unauthorized_action_count": unauthorized,
        "duplicate_refund_count": duplicate_effects,
        "policy_retrieval_quality": round(policy_hits / len(policy_required), 3),
        "policy_retrieval_cases": len(policy_required),
        "held_out": {
            "cases_total": len(held_out),
            "cases_passed": sum(1 for row in held_out if row["passed"]),
            "correctness": round(sum(1 for row in held_out if row["passed"]) / len(held_out), 3),
            "split_rule": "Every fifth case by predeclared case ID; synthetic development check only.",
        },
        "split_counts": dict(splits),
        "category_counts": dict(categories),
        "human_assisted_cases": sum(1 for row in results if row.get("human_assisted")),
        "fully_automated_order_status_cases": sum(1 for row in results if row["category"] == "order_status" and row.get("verified_outcome")),
        "operational_metrics": {
            "mean_case_elapsed_ms": round(sum(row["elapsed_ms"] for row in results) / len(results), 2),
            "model_calls": 0,
            "external_payment_provider_operations": len(provider_rows),
            "external_ticketing_provider_operations": len(ticketing_rows),
            "ticketing_sync_pending_tickets": ticketing_pending,
            "human_wait_time_measured": False,
            "estimated_model_cost": None,
        },
        "classifier_mode": "rules_fallback",
        "cases": results,
        "limitations": [
            "Synthetic deterministic scenarios only; not production performance evidence.",
            "Held-out labels are a reporting split, not an independently collected production test set.",
            "Provider and application records share one database in this prototype; real HTTP/network failure semantics remain external integration work.",
            "Ticketing sync is tested with deterministic in-process failure injection; a separately deployed ticketing service remains external integration work.",
            "Concurrency assertions validate the database reservation constraint and sequential competing attempts; a live PostgreSQL stress test remains required.",
        ],
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    db.close()
    engine.dispose()


if __name__ == "__main__":
    main()

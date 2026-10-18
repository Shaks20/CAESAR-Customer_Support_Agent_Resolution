from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4
import json
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.db.models import ActionProposal, Approval, Order, Payment, RefundOperation, ReturnRecord, Ticket, WorkflowRun
from app.services.audit import record_event
from app.services.payment_provider import create_refund, get_refund


def check_refund_eligibility(order: Order, returned: ReturnRecord | None, payment: Payment | None) -> tuple[bool, str]:
    if payment is None:
        return False, "No payment record was found, so a refund cannot be validated."
    if payment.status.lower() not in {"paid", "partially_refunded"}:
        return False, f"Payment status is '{payment.status}', so the payment is not eligible for a refund."
    if order.status.lower() not in {"delivered", "completed", "returned", "partially_returned"}:
        return False, f"Order status is '{order.status}', so delivery/return facts need review before refunding."
    if returned is None:
        return False, "No return record exists for this order."
    if returned.status.lower() not in {"received", "completed", "returned"}:
        return False, f"Return status is '{returned.status}', not received or completed."
    if returned.inspection_status.lower() not in {"passed", "pass", "not_required"}:
        return False, f"Return inspection status is '{returned.inspection_status}', so the refund is not eligible."
    if payment.refunded_amount_minor >= order.paid_amount_minor:
        return False, "The order has already been fully refunded."
    return True, "Return received, inspection passed, and refundable balance remains."


def create_proposal(db: Session, run_id: str, order: Order | None, returned: ReturnRecord | None,
                    payment: Payment | None, evidence: list[dict], citations: list[dict],
                    category: str = "other", shipment: dict | None = None,
                    complaint: str = "") -> ActionProposal:
    if not citations:
        action, amount, currency = "escalate", 0, order.currency if order else "INR"
        rationale = "No applicable policy evidence was retrieved. The case is escalated rather than inferring eligibility or inventing a resolution."
    elif any(citation.get("policy_conflict") for citation in citations):
        action, amount, currency = "escalate", 0, order.currency if order else "INR"
        rationale = "Conflicting active versions of an applicable policy were found. A support specialist must resolve the policy conflict before taking action."
    elif category == "order_status":
        currency = order.currency if order else "INR"
        if order is None:
            action, amount = "escalate", 0
            rationale = "No order was identified. Ask the customer for an order reference before providing status."
        elif not shipment or not shipment.get("available"):
            action, amount = "escalate", 0
            rationale = "The shipping service has no shipment record. The latest status cannot be confirmed, so the case needs review."
        elif shipment.get("is_stale"):
            action, amount = "escalate", 0
            rationale = "The latest shipment record is stale. The delivery status cannot be confirmed, so the case needs review."
        else:
            action, amount = "inform", 0
            rationale = (f"Latest shipment status from the shipping service is '{shipment.get('status')}'. "
                         "No financial action is needed.")
    elif category == "damaged_item":
        action, amount, currency = "request_information", 0, order.currency if order else "INR"
        complaint_text = (complaint or "").lower()
        has_reference = any(marker in complaint_text for marker in (
            "photo reference:", "image reference:", "attachment id:", "photo id:",
        ))
        if order is None:
            rationale = "Please provide the order reference and a concise damage description with a photo/attachment reference. No replacement or refund action will be taken yet."
        elif not has_reference:
            rationale = "Please provide a concise description of the damage and a photo/attachment reference. A support specialist will review it before any replacement or refund is proposed."
        else:
            action = "escalate"
            rationale = "The order and damage evidence reference are recorded. A support specialist must review the case before any replacement or refund is proposed."
    elif category != "refund":
        action, amount, currency = "escalate", 0, order.currency if order else "INR"
        rationale = ("This complaint requires a support specialist to review the applicable workflow. "
                     "No refund was proposed because the complaint was not classified as a refund request.")
    elif order is None:
        action, amount, currency = "escalate", 0, "INR"
        rationale = "No order was identified. Ask the customer for an order reference before taking action."
    else:
        eligible, reason = check_refund_eligibility(order, returned, payment)
        if eligible:
            action, amount, currency = "refund", order.paid_amount_minor - payment.refunded_amount_minor, order.currency
            rationale = reason + " A reviewer must approve this proposal before any refund is executed."
        else:
            action, amount, currency = "escalate", 0, order.currency
            rationale = reason
    workflow_run = db.get(WorkflowRun, run_id)
    workflow_ticket = db.get(Ticket, workflow_run.ticket_id) if workflow_run else None
    payload = {"run_id": run_id, "customer_id": workflow_ticket.customer_id if workflow_ticket else None,
               "action_type": action, "amount_minor": amount, "currency": currency,
               "order_id": order.id if order else None, "rationale": rationale,
               "evidence_json": json.dumps(evidence), "policy_citations_json": json.dumps(citations)}
    digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    proposal = ActionProposal(
        id=str(uuid4()), run_id=run_id, action_type=action, amount_minor=amount, currency=currency,
        rationale=rationale, evidence_json=json.dumps(evidence), policy_citations_json=json.dumps(citations),
        payload_hash=digest, status=("pending_approval" if action == "refund" else "completed" if action == "inform" else "needs_information" if action == "request_information" else "needs_review"),
    )
    db.add(proposal)
    db.flush()
    if action == "refund":
        from datetime import timedelta
        db.add(Approval(id=str(uuid4()), proposal_id=proposal.id, decision="pending",
                        expires_at=datetime.now(timezone.utc) + timedelta(hours=24)))
    record_event(db, run_id, "agent", "proposal_created", {"proposal_id": proposal.id, "action": action})
    return proposal


def execute_approved_refund(db: Session, proposal: ActionProposal, approval: Approval, reviewer_id: str) -> dict:
    """Execute or reconcile one approved refund without creating duplicate provider effects.

    A durable application-side reservation is committed before the provider call. The
    provider is queried by idempotency key before any retry, so an accepted-but-timed-out
    request is reconciled rather than blindly reissued.
    """
    if proposal.action_type != "refund":
        raise ValueError("Only refund proposals can be executed by this operation")
    if approval.decision != "approved" or approval.reviewer_id != reviewer_id:
        raise PermissionError("A valid stored reviewer approval is required")
    expiry = approval.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    decided_at = approval.decided_at
    if decided_at is None:
        if expiry < datetime.now(timezone.utc):
            raise ValueError("Approval has expired")
    else:
        if decided_at.tzinfo is None:
            decided_at = decided_at.replace(tzinfo=timezone.utc)
        if decided_at > expiry:
            raise ValueError("Approval was recorded after it expired")

    run = db.get(WorkflowRun, proposal.run_id)
    ticket = db.get(Ticket, run.ticket_id) if run else None
    if ticket is None:
        raise ValueError("Ticket/workflow record is unavailable")
    order = db.get(Order, ticket.order_id) if ticket and ticket.order_id else None
    if order is None or order.payment is None:
        raise ValueError("Order/payment record is unavailable")
    if order.customer_id != ticket.customer_id:
        raise PermissionError("Order ownership does not match the ticket customer")

    expected_hash = sha256(json.dumps({
        "run_id": proposal.run_id,
        "customer_id": ticket.customer_id,
        "action_type": proposal.action_type,
        "amount_minor": proposal.amount_minor,
        "currency": proposal.currency,
        "order_id": order.id,
        "rationale": proposal.rationale,
        "evidence_json": proposal.evidence_json,
        "policy_citations_json": proposal.policy_citations_json,
    }, sort_keys=True).encode()).hexdigest()
    if expected_hash != proposal.payload_hash:
        raise ValueError("Approved proposal payload has changed")

    key = f"refund:{proposal.id}"
    existing = db.query(RefundOperation).filter_by(proposal_id=proposal.id).one_or_none()
    if existing and existing.status == "succeeded":
        return {"status": existing.status, "provider_reference": existing.provider_reference,
                "amount_minor": existing.amount_minor, "idempotent_replay": True}
    if existing and (existing.order_id != order.id or existing.amount_minor != proposal.amount_minor
                     or existing.idempotency_key != key):
        raise ValueError("Stored refund reservation does not match the approved proposal")

    eligible, reason = check_refund_eligibility(order, order.return_record, order.payment)
    if not eligible:
        raise ValueError(f"Eligibility changed after approval: {reason}")

    reserved_elsewhere = db.query(func.coalesce(func.sum(RefundOperation.amount_minor), 0)).filter(
        RefundOperation.order_id == order.id,
        RefundOperation.status.in_(["pending", "pending_reconciliation"]),
        RefundOperation.proposal_id != proposal.id,
    ).scalar() or 0
    available_minor = order.paid_amount_minor - order.payment.refunded_amount_minor - reserved_elsewhere
    if proposal.amount_minor <= 0 or proposal.amount_minor > available_minor:
        raise ValueError("Approved refund exceeds the currently available refundable balance")

    if existing is None:
        existing = RefundOperation(
            id=str(uuid4()), order_id=order.id, proposal_id=proposal.id,
            amount_minor=proposal.amount_minor, idempotency_key=key, status="pending",
        )
        db.add(existing)
        try:
            # Persist the reservation before the external side effect. The partial unique
            # index permits only one in-flight refund per order, even under concurrent runs.
            db.commit()
            db.refresh(existing)
        except IntegrityError as exc:
            db.rollback()
            raise ValueError("Another refund operation is already in progress for this order") from exc

    # Reconcile first. If the provider accepted a request before a timeout/crash, this
    # returns the existing operation and prevents a second external side effect.
    provider_result = get_refund(key, bind=db.get_bind())
    if provider_result is None:
        try:
            provider_result = create_refund(order.id, proposal.amount_minor, key, bind=db.get_bind())
        except (TimeoutError, ConnectionError) as exc:
            existing.status = "pending_reconciliation"
            record_event(db, proposal.run_id, "system", "refund_outcome_uncertain", {
                "operation_id": existing.id,
                "idempotency_key": key,
                "error_type": type(exc).__name__,
                "message": "Provider outcome is unknown; reconcile before retrying.",
            })
            db.commit()
            return {"status": "pending_reconciliation", "provider_reference": None,
                    "amount_minor": proposal.amount_minor, "idempotent_replay": False}

    if provider_result["order_id"] != order.id or provider_result["amount_minor"] != proposal.amount_minor:
        existing.status = "pending_reconciliation"
        record_event(db, proposal.run_id, "system", "refund_reconciliation_conflict", {
            "operation_id": existing.id, "idempotency_key": key,
        })
        db.commit()
        raise ValueError("Provider operation does not match the approved refund; manual reconciliation required")

    existing.provider_reference = provider_result["provider_reference"]
    existing.status = provider_result["status"]
    if provider_result["status"] != "succeeded":
        db.commit()
        return {"status": provider_result["status"], "provider_reference": provider_result["provider_reference"],
                "amount_minor": proposal.amount_minor, "idempotent_replay": False}

    order.payment.refunded_amount_minor += proposal.amount_minor
    order.payment.status = ("refunded" if order.payment.refunded_amount_minor >= order.paid_amount_minor
                            else "partially_refunded")
    proposal.status = "executed"
    ticket.status = "resolved"
    record_event(db, proposal.run_id, reviewer_id, "refund_executed", {
        "operation_id": existing.id, "provider_reference": existing.provider_reference,
        "amount_minor": existing.amount_minor, "status": existing.status,
    })
    # The caller may commit its run-state update; reservation + provider reference are
    # already durable, so a rollback here remains recoverable with the same idempotency key.
    return {"status": existing.status, "provider_reference": existing.provider_reference,
            "amount_minor": existing.amount_minor, "idempotent_replay": False}

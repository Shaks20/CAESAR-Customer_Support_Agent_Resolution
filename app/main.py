import json
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException, status
from sqlalchemy import and_, or_, text
from sqlalchemy.exc import SQLAlchemyError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.observability import request_observability_middleware
from app.db.database import get_db
from app.db.models import ActionProposal, Approval, AuditEvent, Customer, Order, RefundOperation, Ticket, TicketMessage, WorkflowRun
from app.schemas.api import (ApprovalDecision, AuditEventOut, MessageCreate, ProposalOut, RunOut,
                             TicketCreate, TicketMessageOut, TicketOut)
from app.services.audit import record_event
from app.services.resolution import execute_approved_refund
from app.services.ticketing_sync import sync_ticket_status
from app.agents.workflow import resume_resolution, start_resolution

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "app" / "static"
MAX_WORKFLOW_RUNS_PER_TICKET = 5
MAX_CUSTOMER_FOLLOWUPS_PER_TICKET = 4
MAX_TICKET_COMPLAINT_CHARS = 12000

app = FastAPI(title="Customer Support Resolution Agent", version="0.1.0",
              description="Evidence-grounded support workflows with controlled actions and auditable outcomes.")
app.middleware("http")(request_observability_middleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def require_customer(x_customer_id: str | None, db: Session) -> Customer:
    if not x_customer_id:
        raise HTTPException(status_code=401, detail="Provide X-Customer-ID for this demo.")
    customer = db.get(Customer, x_customer_id)
    if not customer:
        raise HTTPException(status_code=401, detail="Unknown customer identity.")
    return customer


def require_reviewer(x_reviewer_token: str | None, x_reviewer_id: str | None) -> str:
    if not x_reviewer_token or x_reviewer_token != settings.demo_reviewer_token:
        raise HTTPException(status_code=403, detail="Valid reviewer token required.")
    if not x_reviewer_id or len(x_reviewer_id) > 120:
        raise HTTPException(status_code=400, detail="X-Reviewer-ID is required.")
    return x_reviewer_id


def proposal_out(proposal: ActionProposal) -> ProposalOut:
    return ProposalOut(id=proposal.id, action_type=proposal.action_type,
        amount_minor=proposal.amount_minor, currency=proposal.currency, rationale=proposal.rationale,
        evidence=json.loads(proposal.evidence_json), policy_citations=json.loads(proposal.policy_citations_json),
        status=proposal.status)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database connectivity check failed.") from exc
    return {"status": "ok", "environment": settings.app_env, "database": "connected"}


@app.get("/demo/customers")
def demo_customers(db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name} for c in db.query(Customer).order_by(Customer.name).all()]


@app.get("/demo/orders")
def demo_orders(x_customer_id: str | None = Header(default=None), db: Session = Depends(get_db)):
    customer = require_customer(x_customer_id, db)
    orders = db.query(Order).filter_by(customer_id=customer.id).all()
    return [{"id": o.id, "product_name": o.product_name, "status": o.status,
             "amount_minor": o.paid_amount_minor, "currency": o.currency,
             "return_status": o.return_record.status if o.return_record else None,
             "inspection_status": o.return_record.inspection_status if o.return_record else None}
            for o in orders]


@app.post("/tickets", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(payload: TicketCreate, x_customer_id: str | None = Header(default=None),
                  db: Session = Depends(get_db)):
    customer = require_customer(x_customer_id, db)
    if payload.order_id:
        order = db.get(Order, payload.order_id)
        if not order or order.customer_id != customer.id:
            # Same response for nonexistent and foreign-owned orders to avoid ID enumeration.
            raise HTTPException(status_code=404, detail="Order not found for this customer.")
    ticket = Ticket(id=str(uuid4()), customer_id=customer.id, order_id=payload.order_id,
                    complaint=payload.complaint.strip())
    db.add(ticket)
    db.flush()
    db.add(TicketMessage(id=str(uuid4()), ticket_id=ticket.id, sender="customer", message=payload.complaint.strip()))
    db.commit()
    db.refresh(ticket)
    return ticket


@app.post("/tickets/{ticket_id}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
def run_ticket(ticket_id: str, x_customer_id: str | None = Header(default=None),
               db: Session = Depends(get_db)):
    customer = require_customer(x_customer_id, db)
    ticket = db.get(Ticket, ticket_id)
    if not ticket or ticket.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="Ticket not found for this customer.")
    if ticket.status not in {"open", "needs_review"}:
        raise HTTPException(status_code=409, detail=f"Ticket cannot be run from status '{ticket.status}'.")
    run_count = db.query(WorkflowRun).filter_by(ticket_id=ticket.id).count()
    if run_count >= MAX_WORKFLOW_RUNS_PER_TICKET:
        raise HTTPException(status_code=409, detail="Workflow run limit reached; escalate to a support specialist.")
    run = start_resolution(db, ticket, customer)
    return RunOut(id=run.id, ticket_id=run.ticket_id, status=run.status,
                  summary=run.summary, proposal_id=run.proposal_id,
                  current_step=run.current_step, last_error=run.last_error)


@app.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: str, x_customer_id: str | None = Header(default=None), db: Session = Depends(get_db)):
    customer = require_customer(x_customer_id, db)
    run = db.get(WorkflowRun, run_id)
    ticket = db.get(Ticket, run.ticket_id) if run else None
    if not run or not ticket or ticket.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="Run not found for this customer.")
    return RunOut(id=run.id, ticket_id=run.ticket_id, status=run.status,
                  summary=run.summary, proposal_id=run.proposal_id,
                  current_step=run.current_step, last_error=run.last_error)


@app.get("/tickets/{ticket_id}/runs/latest", response_model=RunOut)
def get_latest_run(ticket_id: str, x_customer_id: str | None = Header(default=None), db: Session = Depends(get_db)):
    customer = require_customer(x_customer_id, db)
    ticket = db.get(Ticket, ticket_id)
    if not ticket or ticket.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="Ticket not found for this customer.")
    run = db.query(WorkflowRun).filter_by(ticket_id=ticket.id).order_by(WorkflowRun.created_at.desc()).first()
    if not run:
        raise HTTPException(status_code=404, detail="No workflow run exists for this ticket.")
    return RunOut(id=run.id, ticket_id=run.ticket_id, status=run.status,
                  summary=run.summary, proposal_id=run.proposal_id,
                  current_step=run.current_step, last_error=run.last_error)


@app.post("/runs/{run_id}/resume", response_model=RunOut)
def resume_workflow_run(run_id: str, x_reviewer_token: str | None = Header(default=None),
                        x_reviewer_id: str | None = Header(default=None), db: Session = Depends(get_db)):
    reviewer_id = require_reviewer(x_reviewer_token, x_reviewer_id)
    run = db.get(WorkflowRun, run_id)
    ticket = db.get(Ticket, run.ticket_id) if run else None
    customer = db.get(Customer, ticket.customer_id) if ticket else None
    if not run or not ticket or not customer:
        raise HTTPException(status_code=404, detail="Workflow run not found.")
    if run.status not in {"failed", "running"}:
        raise HTTPException(status_code=409, detail="Only failed or interrupted workflows can be resumed.")
    try:
        resumed = resume_resolution(db, run, ticket, customer, actor=reviewer_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunOut(id=resumed.id, ticket_id=resumed.ticket_id, status=resumed.status,
                  summary=resumed.summary, proposal_id=resumed.proposal_id,
                  current_step=resumed.current_step, last_error=resumed.last_error)


@app.get("/runs/{run_id}/proposal", response_model=ProposalOut)
def get_proposal(run_id: str, x_customer_id: str | None = Header(default=None), db: Session = Depends(get_db)):
    customer = require_customer(x_customer_id, db)
    run = db.get(WorkflowRun, run_id)
    ticket = db.get(Ticket, run.ticket_id) if run else None
    if not run or not ticket or ticket.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="Run not found for this customer.")
    proposal = db.get(ActionProposal, run.proposal_id) if run.proposal_id else None
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found.")
    return proposal_out(proposal)


@app.get("/runs/{run_id}/events", response_model=list[AuditEventOut])
def get_events(run_id: str, x_customer_id: str | None = Header(default=None), db: Session = Depends(get_db)):
    customer = require_customer(x_customer_id, db)
    run = db.get(WorkflowRun, run_id)
    ticket = db.get(Ticket, run.ticket_id) if run else None
    if not run or not ticket or ticket.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="Run not found for this customer.")
    events = db.query(AuditEvent).filter_by(run_id=run_id).order_by(AuditEvent.created_at).all()
    return [AuditEventOut(id=e.id, actor=e.actor, event_type=e.event_type,
                          details=json.loads(e.details), created_at=e.created_at) for e in events]


@app.get("/approvals")
def list_approvals(x_reviewer_token: str | None = Header(default=None),
                   x_reviewer_id: str | None = Header(default=None), db: Session = Depends(get_db)):
    reviewer_id = require_reviewer(x_reviewer_token, x_reviewer_id)
    rows = db.query(Approval, ActionProposal, WorkflowRun, Ticket).join(
        ActionProposal, Approval.proposal_id == ActionProposal.id).join(
        WorkflowRun, ActionProposal.run_id == WorkflowRun.id).join(
        Ticket, WorkflowRun.ticket_id == Ticket.id).filter(or_(
            Approval.decision == "pending",
            and_(Approval.decision == "approved", Approval.reviewer_id == reviewer_id,
                 ActionProposal.status == "pending_approval"),
        )).all()
    return [{"approval_id": a.id, "proposal": proposal_out(p).model_dump(), "run_id": r.id,
             "ticket_id": t.id, "complaint": t.complaint, "customer_id": t.customer_id,
             "order_id": t.order_id, "expires_at": a.expires_at}
            for a, p, r, t in rows]


@app.post("/approvals/{approval_id}/decision")
def decide_approval(approval_id: str, payload: ApprovalDecision,
                    x_reviewer_token: str | None = Header(default=None),
                    x_reviewer_id: str | None = Header(default=None), db: Session = Depends(get_db)):
    reviewer_id = require_reviewer(x_reviewer_token, x_reviewer_id)
    approval = db.get(Approval, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found.")

    # A repeated approve request from the same reviewer is a reconciliation attempt,
    # not a second approval. This supports recovery after a provider timeout or lost response.
    retrying_approved = (
        approval.decision == "approved"
        and payload.decision == "approve"
        and approval.reviewer_id == reviewer_id
    )
    if approval.decision != "pending" and not retrying_approved:
        raise HTTPException(status_code=409, detail="This approval has already been decided.")

    proposal = db.get(ActionProposal, approval.proposal_id)
    run = db.get(WorkflowRun, proposal.run_id) if proposal else None
    ticket = db.get(Ticket, run.ticket_id) if run else None
    if not proposal or not run or not ticket:
        raise HTTPException(status_code=409, detail="Approval is missing its workflow records.")

    if approval.decision == "pending":
        expiry = approval.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry < datetime.now(timezone.utc):
            approval.decision = "expired"
            proposal.status = "expired"
            run.status = "needs_review"
            ticket.status = "needs_review"
            record_event(db, run.id, reviewer_id, "approval_expired", {"proposal_id": proposal.id})
            db.commit()
            sync_ticket_status(db, ticket, run, actor=reviewer_id)
            db.commit()
            raise HTTPException(status_code=409, detail="Approval expired; start a new run to create a fresh proposal.")

        approval.reviewer_id = reviewer_id
        approval.decided_at = datetime.now(timezone.utc)
        if payload.decision == "reject":
            approval.decision = "rejected"
            proposal.status = "rejected"
            run.status = "rejected"
            ticket.status = "escalated"
            record_event(db, run.id, reviewer_id, "approval_rejected", {
                "proposal_id": proposal.id, "reason": payload.reason or "",
            })
            db.commit()
            sync_result = sync_ticket_status(db, ticket, run, actor=reviewer_id)
            db.commit()
            return {"status": "rejected", "run_id": run.id, "ticketing_sync_status": sync_result["status"],
                    "message": "Refund was not executed; case was escalated."}

        approval.decision = "approved"
        record_event(db, run.id, reviewer_id, "approval_approved", {"proposal_id": proposal.id})
        # Persist the reviewer decision before any external side effect. The refund service
        # separately commits a durable operation reservation before calling the provider.
        db.commit()
        db.refresh(approval)
        db.refresh(proposal)
        db.refresh(run)
        db.refresh(ticket)

    try:
        result = execute_approved_refund(db, proposal, approval, reviewer_id)
        if result["status"] == "succeeded":
            run.status = "resolved"
            ticket.status = "resolved"
            run.summary = f"Refund provider status: succeeded. Reference: {result['provider_reference']}."
        elif result["status"] == "pending_reconciliation":
            run.status = "pending_reconciliation"
            ticket.status = "pending_reconciliation"
            run.summary = "The refund outcome is uncertain. The operation is saved and must be reconciled before a final claim."
        else:
            run.status = "processing"
            ticket.status = "processing"
            run.summary = f"Refund provider status: {result['status']}. The final outcome is not yet confirmed."
        db.commit()
        sync_result = sync_ticket_status(db, ticket, run, actor=reviewer_id)
        db.commit()
        return {"status": run.status, "run_id": run.id, "result": result,
                "ticketing_sync_status": sync_result["status"]}
    except (ValueError, PermissionError) as exc:
        db.rollback()
        # A failed post-approval revalidation is not retryable with the same proposal.
        # Invalidate it and route the case to review so the customer/reviewer is not stuck
        # with an approved proposal that can never execute.
        stored_approval = db.get(Approval, approval_id)
        stored_proposal = db.get(ActionProposal, stored_approval.proposal_id) if stored_approval else None
        stored_run = db.get(WorkflowRun, stored_proposal.run_id) if stored_proposal else None
        stored_ticket = db.get(Ticket, stored_run.ticket_id) if stored_run else None
        if stored_approval and stored_approval.decision == "approved" and stored_proposal and stored_run and stored_ticket:
            active_operation = db.query(RefundOperation).filter(
                RefundOperation.order_id == stored_ticket.order_id,
                RefundOperation.status.in_(["pending", "pending_reconciliation"]),
            ).first() if stored_ticket.order_id else None
            stored_approval.decision = "invalidated"
            stored_proposal.status = "invalidated"
            blocked_status = "pending_reconciliation" if active_operation else "needs_review"
            stored_run.status = blocked_status
            stored_ticket.status = blocked_status
            record_event(db, stored_run.id, reviewer_id, "refund_execution_blocked", {
                "proposal_id": stored_proposal.id,
                "error_type": type(exc).__name__,
                "reason": str(exc)[:240],
                "active_operation_id": active_operation.id if active_operation else None,
            })
            db.commit()
            sync_ticket_status(db, stored_ticket, stored_run, actor=reviewer_id)
            db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/runs/{run_id}/messages", response_model=RunOut, status_code=status.HTTP_201_CREATED)
def add_customer_message(run_id: str, payload: MessageCreate,
                         x_customer_id: str | None = Header(default=None), db: Session = Depends(get_db)):
    customer = require_customer(x_customer_id, db)
    run = db.get(WorkflowRun, run_id)
    ticket = db.get(Ticket, run.ticket_id) if run else None
    if not run or not ticket or ticket.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="Run not found for this customer.")
    latest_run = db.query(WorkflowRun).filter_by(ticket_id=ticket.id).order_by(WorkflowRun.created_at.desc()).first()
    if latest_run is None or latest_run.id != run.id:
        raise HTTPException(status_code=409, detail="Follow-up messages must target the latest workflow run.")
    if run.status != "needs_information" or ticket.status != "needs_information":
        raise HTTPException(status_code=409, detail="This workflow is not waiting for customer information.")
    followup_count = db.query(TicketMessage).filter_by(ticket_id=ticket.id, sender="customer").count() - 1
    if followup_count >= MAX_CUSTOMER_FOLLOWUPS_PER_TICKET:
        raise HTTPException(status_code=409, detail="Customer follow-up limit reached; escalate to a support specialist.")
    run_count = db.query(WorkflowRun).filter_by(ticket_id=ticket.id).count()
    if run_count >= MAX_WORKFLOW_RUNS_PER_TICKET:
        raise HTTPException(status_code=409, detail="Workflow run limit reached; escalate to a support specialist.")

    if payload.order_id:
        order = db.get(Order, payload.order_id)
        if not order or order.customer_id != customer.id:
            raise HTTPException(status_code=404, detail="Order not found for this customer.")
        if ticket.order_id and ticket.order_id != order.id:
            raise HTTPException(status_code=409, detail="A different order is already attached to this ticket.")
        ticket.order_id = order.id

    message = payload.message.strip()
    if len(ticket.complaint) + len(message) + len("\n\nCustomer follow-up: ") > MAX_TICKET_COMPLAINT_CHARS:
        raise HTTPException(status_code=413, detail="Conversation is too long; escalate to a support specialist.")
    db.add(TicketMessage(id=str(uuid4()), ticket_id=ticket.id, sender="customer", message=message))
    ticket.complaint = f"{ticket.complaint}\n\nCustomer follow-up: {message}"
    ticket.status = "open"
    db.commit()
    new_run = start_resolution(db, ticket, customer)
    return RunOut(id=new_run.id, ticket_id=new_run.ticket_id, status=new_run.status,
                  summary=new_run.summary, proposal_id=new_run.proposal_id)


@app.get("/tickets/{ticket_id}/messages", response_model=list[TicketMessageOut])
def list_ticket_messages(ticket_id: str, x_customer_id: str | None = Header(default=None),
                         db: Session = Depends(get_db)):
    customer = require_customer(x_customer_id, db)
    ticket = db.get(Ticket, ticket_id)
    if not ticket or ticket.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="Ticket not found for this customer.")
    messages = db.query(TicketMessage).filter_by(ticket_id=ticket_id).order_by(TicketMessage.created_at).all()
    return messages


@app.post("/runs/{run_id}/ticket-sync")
def retry_ticket_sync(run_id: str, x_reviewer_token: str | None = Header(default=None),
                      x_reviewer_id: str | None = Header(default=None), db: Session = Depends(get_db)):
    reviewer_id = require_reviewer(x_reviewer_token, x_reviewer_id)
    run = db.get(WorkflowRun, run_id)
    ticket = db.get(Ticket, run.ticket_id) if run else None
    if not run or not ticket:
        raise HTTPException(status_code=404, detail="Workflow run not found.")
    result = sync_ticket_status(db, ticket, run, actor=reviewer_id)
    db.commit()
    return result


@app.get("/tickets/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: str, x_customer_id: str | None = Header(default=None), db: Session = Depends(get_db)):
    customer = require_customer(x_customer_id, db)
    ticket = db.get(Ticket, ticket_id)
    if not ticket or ticket.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="Ticket not found for this customer.")
    return ticket

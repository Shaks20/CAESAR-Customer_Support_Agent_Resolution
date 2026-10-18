"""Checkpointed support workflow with LangGraph and deterministic fallback execution."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TypedDict
from uuid import uuid4

from sqlalchemy.orm import Session

from app.agents.classifier import classify_complaint
from app.db.models import ActionProposal, Customer, Order, Ticket, WorkflowRun
from app.services.audit import record_event
from app.services.policies import retrieve_policies
from app.services.resolution import create_proposal
from app.services.shipping_provider import get_shipment_status
from app.services.ticketing_sync import sync_ticket_status

STEP_ORDER = {"created": 0, "classify": 1, "investigate": 2, "retrieve_policy": 3, "propose": 4}
BASE_STATE_KEYS = {"ticket_id", "customer_id", "complaint", "run_id", "checkpoint_step"}


class SupportState(TypedDict, total=False):
    ticket_id: str
    customer_id: str
    complaint: str
    category: str
    classification_source: str
    order_id: str | None
    shipment: dict
    evidence: list[dict]
    citations: list[dict]
    summary: str
    run_id: str
    proposal_id: str
    status: str
    checkpoint_step: str


def _step_completed(state: SupportState, step: str) -> bool:
    current = state.get("checkpoint_step", "created")
    return STEP_ORDER.get(current, 0) >= STEP_ORDER[step]


def _save_checkpoint(db: Session, state: SupportState, updates: dict, step: str) -> dict:
    """Persist a minimal, resumable snapshot after a node finishes successfully."""
    merged = dict(state)
    merged.update(updates)
    merged["checkpoint_step"] = step
    snapshot = {key: value for key, value in merged.items() if key not in BASE_STATE_KEYS}
    run = db.get(WorkflowRun, state["run_id"])
    run.current_step = step
    run.state_json = json.dumps(snapshot, sort_keys=True, default=str)
    run.updated_at = datetime.now(timezone.utc)
    record_event(db, run.id, "workflow", "checkpoint_saved", {
        "step": step, "state_keys": sorted(snapshot.keys()),
    })
    db.commit()
    return {**updates, "checkpoint_step": step}


def build_workflow(db: Session):
    def classify(state: SupportState) -> dict:
        if _step_completed(state, "classify"):
            return {}
        category, source = classify_complaint(state["complaint"])
        return _save_checkpoint(db, state, {
            "category": category, "classification_source": source,
        }, "classify")

    def investigate(state: SupportState) -> dict:
        if _step_completed(state, "investigate"):
            return {}
        ticket = db.get(Ticket, state["ticket_id"])
        order = db.get(Order, ticket.order_id) if ticket and ticket.order_id else None
        if order is not None and order.customer_id != state["customer_id"]:
            record_event(db, state["run_id"], "authorization", "order_ownership_denied", {
                "ticket_id": ticket.id,
            })
            order = None
        evidence: list[dict] = []
        if order:
            evidence.append({"source": "orders", "record_id": order.id, "fact": "product_name", "value": order.product_name})
            evidence.append({"source": "orders", "record_id": order.id, "fact": "paid_amount_minor", "value": order.paid_amount_minor})
            try:
                shipment = get_shipment_status(order.id, db_session=db)
            except (ConnectionError, TimeoutError) as exc:
                shipment = {"available": False, "status": None, "tracking_number": None,
                            "updated_at": None, "is_stale": False}
                record_event(db, state["run_id"], "shipping_provider", "shipping_lookup_failed", {
                    "order_id": order.id, "error_type": type(exc).__name__,
                })
            if shipment.get("available"):
                evidence.append({"source": "shipments", "record_id": order.id, "fact": "status", "value": shipment.get("status")})
                evidence.append({"source": "shipments", "record_id": order.id, "fact": "tracking_number", "value": shipment.get("tracking_number")})
                evidence.append({"source": "shipments", "record_id": order.id, "fact": "is_stale", "value": shipment.get("is_stale")})
            if order.return_record:
                evidence.append({"source": "returns", "record_id": order.return_record.id, "fact": "status", "value": order.return_record.status})
                evidence.append({"source": "returns", "record_id": order.return_record.id, "fact": "inspection_status", "value": order.return_record.inspection_status})
            if order.payment:
                evidence.append({"source": "payments", "record_id": order.payment.id, "fact": "refunded_amount_minor", "value": order.payment.refunded_amount_minor})
            if not shipment.get("available"):
                evidence.append({"source": "shipments", "record_id": order.id, "fact": "availability", "value": "unavailable"})
        else:
            shipment = {"available": False, "status": None, "tracking_number": None,
                        "updated_at": None, "is_stale": False}
            evidence.append({"source": "system", "record_id": None, "fact": "order_lookup", "value": "No order record identified"})
        return _save_checkpoint(db, state, {
            "order_id": order.id if order else None, "evidence": evidence, "shipment": shipment,
        }, "investigate")

    def retrieve_policy(state: SupportState) -> dict:
        if _step_completed(state, "retrieve_policy"):
            return {}
        query = f'{state["category"]} {state["complaint"]}'
        if state.get("order_id"):
            query += " order return payment eligibility"
        citations = retrieve_policies(query, db=db)
        return _save_checkpoint(db, state, {"citations": citations}, "retrieve_policy")

    def propose(state: SupportState) -> dict:
        if _step_completed(state, "propose"):
            return {}
        ticket = db.get(Ticket, state["ticket_id"])
        order = db.get(Order, state.get("order_id")) if state.get("order_id") else None
        run = db.get(WorkflowRun, state["run_id"])
        proposal = create_proposal(db, run.id, order, order.return_record if order else None,
                                  order.payment if order else None, state.get("evidence", []),
                                  state.get("citations", []), category=state["category"],
                                  shipment=state.get("shipment"), complaint=state["complaint"])
        summary = (f"Classified as {state['category']}. "
                   f"Proposed action: {proposal.action_type}. {proposal.rationale}")
        if proposal.action_type == "refund":
            run.status, ticket.status = "awaiting_approval", "awaiting_approval"
        elif proposal.action_type == "inform":
            run.status, ticket.status = "resolved", "resolved"
        elif proposal.action_type == "request_information":
            run.status, ticket.status = "needs_information", "needs_information"
        else:
            run.status, ticket.status = "needs_review", "needs_review"
        run.summary = summary
        run.proposal_id = proposal.id
        ticket.category = state["category"]
        record_event(db, run.id, "agent", "investigation_completed", {
            "category": state["category"], "classification_source": state.get("classification_source", "unknown"),
            "evidence_count": len(state.get("evidence", [])),
            "policy_citations_count": len(state.get("citations", [])),
        })
        return _save_checkpoint(db, state, {
            "proposal_id": proposal.id, "summary": summary, "status": run.status,
        }, "propose")

    try:
        from langgraph.graph import END, START, StateGraph
        graph = StateGraph(SupportState)
        graph.add_node("classify", classify)
        graph.add_node("investigate", investigate)
        graph.add_node("retrieve_policy", retrieve_policy)
        graph.add_node("propose", propose)
        graph.add_edge(START, "classify")
        graph.add_edge("classify", "investigate")
        graph.add_edge("investigate", "retrieve_policy")
        graph.add_edge("retrieve_policy", "propose")
        graph.add_edge("propose", END)
        return graph.compile()
    except ImportError:
        class SimpleWorkflow:
            def invoke(self, state: SupportState) -> dict:
                state.update(classify(state))
                state.update(investigate(state))
                state.update(retrieve_policy(state))
                state.update(propose(state))
                return state
        return SimpleWorkflow()


def _execute_from_checkpoint(db: Session, run: WorkflowRun, ticket: Ticket, customer: Customer,
                             state: SupportState) -> WorkflowRun:
    try:
        build_workflow(db).invoke(state)
        db.commit()
        db.refresh(run)
        db.refresh(ticket)
        if run.proposal_id:
            proposal = db.get(ActionProposal, run.proposal_id)
            if proposal:
                if proposal.action_type == "refund" and proposal.status == "pending_approval":
                    run.status = ticket.status = "awaiting_approval"
                elif proposal.action_type == "inform":
                    run.status = ticket.status = "resolved"
                elif proposal.action_type == "request_information":
                    run.status = ticket.status = "needs_information"
                else:
                    run.status = ticket.status = "needs_review"
                db.commit()
        sync_ticket_status(db, ticket, run, actor="workflow")
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        db.rollback()
        run = db.get(WorkflowRun, run.id)
        ticket = db.get(Ticket, ticket.id)
        run.status = "failed"
        run.last_error = type(exc).__name__
        run.summary = f"Workflow failed after checkpoint '{run.current_step}'. A reviewer can resume from the last saved step."
        ticket.status = "needs_review"
        record_event(db, run.id, "workflow", "workflow_failed", {
            "last_completed_step": run.current_step, "error_type": type(exc).__name__,
        })
        db.commit()
        sync_ticket_status(db, ticket, run, actor="workflow")
        db.commit()
        db.refresh(run)
        return run


def start_resolution(db: Session, ticket: Ticket, customer: Customer) -> WorkflowRun:
    run = WorkflowRun(id=str(uuid4()), ticket_id=ticket.id, status="running", current_step="created", state_json="{}")
    db.add(run)
    db.flush()
    record_event(db, run.id, customer.id, "workflow_started", {"ticket_id": ticket.id})
    db.commit()  # Persist the run before work begins so a process restart can resume it.
    db.refresh(run)
    state: SupportState = {"ticket_id": ticket.id, "customer_id": customer.id,
                           "complaint": ticket.complaint, "run_id": run.id,
                           "checkpoint_step": run.current_step}
    return _execute_from_checkpoint(db, run, ticket, customer, state)


def resume_resolution(db: Session, run: WorkflowRun, ticket: Ticket, customer: Customer,
                      actor: str | None = None) -> WorkflowRun:
    if run.status not in {"failed", "running"}:
        raise ValueError("Only failed or interrupted workflows can be resumed.")
    try:
        saved_state = json.loads(run.state_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Stored workflow checkpoint is invalid; manual review is required.") from exc
    state: SupportState = {"ticket_id": ticket.id, "customer_id": customer.id,
                           "complaint": ticket.complaint, "run_id": run.id,
                           "checkpoint_step": run.current_step, **saved_state}
    run.status = "running"
    run.last_error = None
    record_event(db, run.id, actor or customer.id, "workflow_resume_started", {
        "checkpoint_step": run.current_step,
    })
    db.commit()
    return _execute_from_checkpoint(db, run, ticket, customer, state)

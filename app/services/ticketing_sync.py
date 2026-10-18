"""Synchronize local ticket state to the mock external ticketing API."""
from sqlalchemy.orm import Session

from app.db.models import Ticket, WorkflowRun
from app.services.audit import record_event
from app.services.ticketing_provider import get_ticket_update, update_ticket_status


def sync_ticket_status(db: Session, ticket: Ticket, run: WorkflowRun, actor: str = "system") -> dict:
    """Idempotently sync one workflow status; preserve a retryable pending state on outages."""
    key = f"ticket:{ticket.id}:{run.id}:{ticket.status}"
    try:
        result = get_ticket_update(key, bind=db.get_bind())
        if result is None:
            result = update_ticket_status(ticket.id, ticket.status, key, bind=db.get_bind())
    except (ConnectionError, TimeoutError) as exc:
        ticket.ticketing_sync_status = "pending"
        record_event(db, run.id, actor, "ticketing_sync_pending", {
            "ticket_id": ticket.id, "status": ticket.status, "idempotency_key": key,
            "error_type": type(exc).__name__,
        })
        db.flush()
        return {"status": "pending", "ticket_id": ticket.id, "idempotency_key": key}

    ticket.ticketing_sync_status = "synced"
    record_event(db, run.id, actor, "ticketing_sync_succeeded", {
        "ticket_id": ticket.id, "status": result["status"],
        "provider_reference": result["provider_reference"], "idempotency_key": key,
    })
    db.flush()
    return {"status": "synced", "ticket_id": result["ticket_id"],
            "external_status": result["status"], "provider_reference": result["provider_reference"],
            "idempotency_key": key}

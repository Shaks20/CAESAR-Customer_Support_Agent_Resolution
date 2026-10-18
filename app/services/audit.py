import json
from uuid import uuid4
from sqlalchemy.orm import Session
from app.db.models import AuditEvent


def record_event(db: Session, run_id: str, actor: str, event_type: str, details: dict | None = None) -> None:
    db.add(AuditEvent(
        id=str(uuid4()), run_id=run_id, actor=actor, event_type=event_type,
        details=json.dumps(details or {}, sort_keys=True),
    ))
    db.flush()

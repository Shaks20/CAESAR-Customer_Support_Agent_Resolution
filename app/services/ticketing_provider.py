"""Idempotent mock ticketing API with deterministic failure/latency injection."""
from __future__ import annotations

import os
import time
from hashlib import sha256
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.database import SessionLocal
from app.db.models import MockTicketingOperation


def _session_factory(bind=None):
    return sessionmaker(bind=bind, expire_on_commit=False) if bind is not None else SessionLocal


def get_ticket_update(idempotency_key: str, *, bind=None) -> dict | None:
    """Read the provider's durable record before retrying an uncertain update."""
    db = _session_factory(bind)()
    try:
        operation = db.query(MockTicketingOperation).filter_by(idempotency_key=idempotency_key).one_or_none()
        if operation is None:
            return None
        return {"ticket_id": operation.ticket_id, "status": operation.status,
                "provider_reference": operation.provider_reference}
    finally:
        db.close()


def update_ticket_status(ticket_id: str, status: str, idempotency_key: str, *, bind=None,
                         simulate_error: bool = False, delay_ms: int | None = None,
                         timeout_after_accept: bool = False) -> dict:
    """Update the simulated external ticketing system once per key and payload.

    `MOCK_TICKETING_FAIL=1` injects a pre-acceptance outage; `MOCK_TICKETING_DELAY_MS`
    injects bounded latency. `timeout_after_accept` supports deterministic recovery tests.
    """
    try:
        configured_delay = int(os.getenv("MOCK_TICKETING_DELAY_MS", "0")) if delay_ms is None else delay_ms
    except ValueError:
        configured_delay = 0
    time.sleep(min(max(configured_delay, 0), 10000) / 1000)
    if simulate_error or os.getenv("MOCK_TICKETING_FAIL", "0") == "1":
        raise ConnectionError("Mock ticketing provider is unavailable")
    timeout_after_accept = timeout_after_accept or os.getenv("MOCK_TICKETING_TIMEOUT_AFTER_ACCEPT", "0") == "1"

    fingerprint = sha256(f"{ticket_id}:{status}".encode()).hexdigest()
    db = _session_factory(bind)()
    try:
        existing = db.query(MockTicketingOperation).filter_by(idempotency_key=idempotency_key).one_or_none()
        if existing:
            if existing.payload_fingerprint != fingerprint:
                raise ValueError("Ticketing idempotency key was reused with a different payload")
            result = {"ticket_id": existing.ticket_id, "status": existing.status,
                      "provider_reference": existing.provider_reference}
            if timeout_after_accept:
                raise TimeoutError("Mock ticketing provider accepted the update but the response was lost")
            return result
        operation = MockTicketingOperation(
            id=str(uuid4()), idempotency_key=idempotency_key, payload_fingerprint=fingerprint,
            ticket_id=ticket_id, status=status, provider_reference=f"mock_ticket_{uuid4().hex[:12]}",
        )
        db.add(operation)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.query(MockTicketingOperation).filter_by(idempotency_key=idempotency_key).one()
            if existing.payload_fingerprint != fingerprint:
                raise ValueError("Ticketing idempotency key was reused with a different payload")
            result = {"ticket_id": existing.ticket_id, "status": existing.status,
                      "provider_reference": existing.provider_reference}
        else:
            result = {"ticket_id": operation.ticket_id, "status": operation.status,
                      "provider_reference": operation.provider_reference}
        if timeout_after_accept:
            raise TimeoutError("Mock ticketing provider accepted the update but the response was lost")
        return result
    finally:
        db.close()

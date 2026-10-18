"""Durable mock payment provider with a separate committed idempotency record."""
import os
import time
from hashlib import sha256
from uuid import uuid4
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from app.db.database import SessionLocal
from app.db.models import MockProviderOperation


def create_refund(order_id: str, amount_minor: int, idempotency_key: str, *, bind=None) -> dict:
    """Simulate a provider API call. Provider records commit independently from app writes.

    This makes the mock useful for testing the "provider accepted, app response failed" shape.
    It is still a same-database simulation, not a true network boundary.
    """
    try:
        delay_ms = int(os.getenv("MOCK_PAYMENT_DELAY_MS", "0"))
    except ValueError:
        delay_ms = 0
    time.sleep(min(max(delay_ms, 0), 10000) / 1000)
    if os.getenv("MOCK_PAYMENT_FAIL", "0") == "1":
        raise ConnectionError("Mock payment provider is unavailable before acceptance")
    timeout_after_accept = os.getenv("MOCK_PAYMENT_TIMEOUT_AFTER_ACCEPT", "0") == "1"
    payload_fingerprint = sha256(f"{order_id}:{amount_minor}".encode()).hexdigest()
    provider_session = sessionmaker(bind=bind, expire_on_commit=False) if bind is not None else SessionLocal
    db = provider_session()
    try:
        existing = db.query(MockProviderOperation).filter_by(idempotency_key=idempotency_key).one_or_none()
        if existing:
            if existing.payload_fingerprint != payload_fingerprint:
                raise ValueError("Idempotency key was already used with a different payload")
            result = {"provider_reference": existing.provider_reference, "status": existing.status,
                      "order_id": existing.order_id, "amount_minor": existing.amount_minor}
            if timeout_after_accept:
                raise TimeoutError("Mock payment provider accepted the refund but the response was lost")
            return result
        operation = MockProviderOperation(
            id=str(uuid4()), idempotency_key=idempotency_key, payload_fingerprint=payload_fingerprint,
            order_id=order_id, amount_minor=amount_minor, provider_reference=f"mock_ref_{uuid4().hex[:12]}",
            status="succeeded",
        )
        db.add(operation)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            # A concurrent identical request may have won the unique-key race.
            existing = db.query(MockProviderOperation).filter_by(idempotency_key=idempotency_key).one()
            if existing.payload_fingerprint != payload_fingerprint:
                raise ValueError("Idempotency key was already used with a different payload")
            result = {"provider_reference": existing.provider_reference, "status": existing.status,
                      "order_id": existing.order_id, "amount_minor": existing.amount_minor}
            if timeout_after_accept:
                raise TimeoutError("Mock payment provider accepted the refund but the response was lost")
            return result
        result = {"provider_reference": operation.provider_reference, "status": operation.status,
                  "order_id": operation.order_id, "amount_minor": operation.amount_minor}
        if timeout_after_accept:
            raise TimeoutError("Mock payment provider accepted the refund but the response was lost")
        return result
    finally:
        db.close()


def get_refund(idempotency_key: str, *, bind=None) -> dict | None:
    """Look up a provider operation before retrying an uncertain request."""
    provider_session = sessionmaker(bind=bind, expire_on_commit=False) if bind is not None else SessionLocal
    db = provider_session()
    try:
        operation = db.query(MockProviderOperation).filter_by(idempotency_key=idempotency_key).one_or_none()
        if operation is None:
            return None
        return {
            "provider_reference": operation.provider_reference,
            "status": operation.status,
            "order_id": operation.order_id,
            "amount_minor": operation.amount_minor,
        }
    finally:
        db.close()


def reset_provider_for_tests(*, bind=None) -> None:
    provider_session = sessionmaker(bind=bind) if bind is not None else SessionLocal
    db = provider_session()
    try:
        db.query(MockProviderOperation).delete()
        db.commit()
    finally:
        db.close()

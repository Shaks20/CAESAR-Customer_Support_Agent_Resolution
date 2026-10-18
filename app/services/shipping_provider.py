"""Mock shipping-service adapter with deterministic delay/failure injection."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, sessionmaker

from app.db.database import SessionLocal
from app.db.models import Shipment


def get_shipment_status(order_id: str, *, db_session: Session | None = None, bind=None,
                        simulate_error: bool = False, delay_ms: int | None = None) -> dict:
    """Read the latest shipment record, returning a structured result.

    `MOCK_SHIPPING_FAIL=1` and `MOCK_SHIPPING_DELAY_MS` make failure/latency scenarios
    reproducible without introducing a real carrier dependency.
    """
    try:
        configured_delay = int(os.getenv("MOCK_SHIPPING_DELAY_MS", "0")) if delay_ms is None else delay_ms
    except ValueError:
        configured_delay = 0
    time.sleep(min(max(configured_delay, 0), 10000) / 1000)
    if simulate_error or os.getenv("MOCK_SHIPPING_FAIL", "0") == "1":
        raise ConnectionError("Mock shipping provider is unavailable")

    owns_session = db_session is None
    if db_session is None:
        session_factory = sessionmaker(bind=bind, expire_on_commit=False) if bind is not None else SessionLocal
        db_session = session_factory()
    try:
        shipment = db_session.query(Shipment).filter_by(order_id=order_id).one_or_none()
        if shipment is None:
            return {"available": False, "status": None, "tracking_number": None,
                    "updated_at": None, "is_stale": False}
        updated_at = shipment.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        stale = datetime.now(timezone.utc) - updated_at > timedelta(hours=24)
        return {"available": True, "status": shipment.status,
                "tracking_number": shipment.tracking_number,
                "updated_at": updated_at.isoformat(), "is_stale": stale}
    finally:
        if owns_session:
            db_session.close()

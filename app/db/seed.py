from datetime import datetime, timezone, timedelta
from app.db.database import SessionLocal
from app.db.models import Customer, Order, Payment, ReturnRecord, Shipment
from app.services.policies import ingest_policy_documents


def seed() -> None:
    db = SessionLocal()
    try:
        ingest_policy_documents(db)
        if db.query(Customer).count():
            return
        customers = [
            Customer(id="cust_ada", name="Aarav Mehta", email="aarav@example.test"),
            Customer(id="cust_grace", name="Diya Sharma", email="diya@example.test"),
        ]
        db.add_all(customers)
        db.flush()
        delivered = datetime.now(timezone.utc) - timedelta(days=8)
        orders = [
            Order(id="ord_1001", customer_id="cust_ada", product_name="Wireless Headphones", status="delivered", currency="INR", paid_amount_minor=429900, delivered_at=delivered),
            Order(id="ord_1002", customer_id="cust_ada", product_name="Bluetooth Speaker", status="delivered", currency="INR", paid_amount_minor=259900, delivered_at=delivered),
            Order(id="ord_2001", customer_id="cust_grace", product_name="Mechanical Keyboard", status="shipped", currency="INR", paid_amount_minor=649900, delivered_at=None),
        ]
        db.add_all(orders)
        db.flush()
        db.add_all([
            Shipment(id="ship_1001", order_id="ord_1001", status="delivered", tracking_number="DEMO-1001", updated_at=datetime.now(timezone.utc)),
            Shipment(id="ship_1002", order_id="ord_1002", status="delivered", tracking_number="DEMO-1002", updated_at=datetime.now(timezone.utc)),
            Shipment(id="ship_2001", order_id="ord_2001", status="shipped", tracking_number="DEMO-2001", updated_at=datetime.now(timezone.utc)),
        ])
        db.add_all([
            ReturnRecord(id="ret_1001", order_id="ord_1001", status="received", inspection_status="passed", received_at=datetime.now(timezone.utc)-timedelta(days=2)),
            ReturnRecord(id="ret_1002", order_id="ord_1002", status="received", inspection_status="failed", received_at=datetime.now(timezone.utc)-timedelta(days=1)),
        ])
        db.add_all([
            Payment(id="pay_1001", order_id="ord_1001", status="paid", refunded_amount_minor=0),
            Payment(id="pay_1002", order_id="ord_1002", status="paid", refunded_amount_minor=0),
            Payment(id="pay_2001", order_id="ord_2001", status="paid", refunded_amount_minor=0),
        ])
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    seed()

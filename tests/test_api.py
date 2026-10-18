from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db.database import Base, get_db
from app.db.models import ActionProposal, Approval, AuditEvent, WorkflowRun, Customer, Order, Payment, ReturnRecord, Shipment, Ticket, TicketMessage
from app.main import app
from app.core.config import settings

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base.metadata.create_all(engine)

def override_get_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def setup_function():
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    db = TestingSession()
    c = Customer(id="cust_a", name="Ada Test", email="ada@test.invalid")
    order = Order(id="order_a", customer_id="cust_a", product_name="Headphones", status="delivered", currency="INR", paid_amount_minor=10000, delivered_at=datetime.now(timezone.utc)-timedelta(days=5))
    ret = ReturnRecord(id="return_a", order_id="order_a", status="received", inspection_status="passed")
    shipment = Shipment(id="shipment_a", order_id="order_a", status="in_transit", tracking_number="TEST-TRACK-1", updated_at=datetime.now(timezone.utc))
    payment = Payment(id="payment_a", order_id="order_a", status="paid", refunded_amount_minor=0)
    db.add_all([c, order, ret, shipment, payment]); db.commit()
    from app.services.policies import ingest_policy_documents
    ingest_policy_documents(db)
    db.close()
    settings.demo_reviewer_token = "test-token"


def test_full_approved_refund_flow():
    headers = {"X-Customer-ID": "cust_a"}
    response = client.post("/tickets", headers=headers, json={"order_id":"order_a", "complaint":"I returned my headphones and need a refund."})
    assert response.status_code == 201, response.text
    ticket = response.json()
    run_response = client.post(f"/tickets/{ticket['id']}/runs", headers=headers)
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()
    assert run["status"] == "awaiting_approval"
    approvals = client.get("/approvals", headers={"X-Reviewer-Token":"test-token", "X-Reviewer-ID":"reviewer-1"})
    assert approvals.status_code == 200
    matching = next(x for x in approvals.json() if x["proposal"]["id"] == run["proposal_id"])
    decision = client.post(f"/approvals/{matching['approval_id']}/decision",
        headers={"X-Reviewer-Token":"test-token", "X-Reviewer-ID":"reviewer-1"},
        json={"decision":"approve", "reason":"Verified in demo"})
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "resolved"
    final_run = client.get(f"/runs/{run['id']}", headers=headers).json()
    assert final_run["status"] == "resolved"
    final_ticket = client.get(f"/tickets/{ticket['id']}", headers=headers).json()
    assert final_ticket["ticketing_sync_status"] == "synced"


def test_cannot_access_another_customers_order():
    db = TestingSession()
    db.add(Customer(id="cust_b", name="Other", email="other@test.invalid")); db.commit(); db.close()
    response = client.post("/tickets", headers={"X-Customer-ID":"cust_b"}, json={"order_id":"order_a", "complaint":"Please check my order and refund."})
    assert response.status_code == 404


def test_reviewer_token_required():
    response = client.get("/approvals", headers={"X-Reviewer-ID":"reviewer-1"})
    assert response.status_code == 403


def test_non_refund_complaint_never_proposes_refund():
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers,
        json={"order_id":"order_a", "complaint":"Where is my order? Please share the delivery status."}).json()
    response = client.post(f"/tickets/{ticket['id']}/runs", headers=headers)
    assert response.status_code == 201, response.text
    run = response.json()
    proposal = client.get(f"/runs/{run['id']}/proposal", headers=headers).json()
    assert proposal["action_type"] == "inform"
    assert proposal["amount_minor"] == 0
    assert any(item["source"] == "shipments" and item["fact"] == "status" and item["value"] == "in_transit" for item in proposal["evidence"])
    assert "in_transit" in proposal["rationale"]
    assert run["status"] == "resolved"


def test_failed_inspection_never_issues_refund():
    db = TestingSession()
    ret = db.query(ReturnRecord).filter_by(order_id="order_a").one()
    ret.inspection_status = "failed"
    db.commit(); db.close()
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers,
        json={"order_id":"order_a", "complaint":"I returned my headphones and need a refund."}).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    proposal = client.get(f"/runs/{run['id']}/proposal", headers=headers).json()
    assert proposal["action_type"] == "escalate"
    assert proposal["amount_minor"] == 0
    assert run["status"] == "needs_review"


def test_rejected_approval_does_not_execute_refund():
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers,
        json={"order_id":"order_a", "complaint":"I returned my headphones and need a refund."}).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    approvals = client.get("/approvals", headers={"X-Reviewer-Token":"test-token", "X-Reviewer-ID":"reviewer-1"}).json()
    approval = next(x for x in approvals if x["proposal"]["id"] == run["proposal_id"])
    response = client.post(f"/approvals/{approval['approval_id']}/decision",
        headers={"X-Reviewer-Token":"test-token", "X-Reviewer-ID":"reviewer-1"},
        json={"decision":"reject", "reason":"Need more evidence"})
    assert response.status_code == 200
    db = TestingSession()
    assert db.query(Payment).filter_by(order_id="order_a").one().refunded_amount_minor == 0
    db.close()


def test_expired_approval_is_blocked():
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers,
        json={"order_id":"order_a", "complaint":"I returned my headphones and need a refund."}).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    approvals = client.get("/approvals", headers={"X-Reviewer-Token":"test-token", "X-Reviewer-ID":"reviewer-1"}).json()
    approval = next(x for x in approvals if x["proposal"]["id"] == run["proposal_id"])
    db = TestingSession()
    row = db.query(Approval).filter_by(id=approval['approval_id']).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit(); db.close()
    response = client.post(f"/approvals/{approval['approval_id']}/decision",
        headers={"X-Reviewer-Token":"test-token", "X-Reviewer-ID":"reviewer-1"},
        json={"decision":"approve"})
    assert response.status_code == 409
    db = TestingSession()
    assert db.get(Approval, approval['approval_id']).decision == "expired"
    assert db.get(Ticket, ticket['id']).status == "needs_review"
    db.close()
    retry_run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers)
    assert retry_run.status_code == 201, retry_run.text


def test_approved_timeout_can_be_retried_to_reconcile_without_duplicate(monkeypatch):
    import app.services.resolution as resolution_service
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers, json={
        "order_id": "order_a", "complaint": "I returned my headphones and need a refund."
    }).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    reviewer_headers = {"X-Reviewer-Token": "test-token", "X-Reviewer-ID": "reviewer-1"}
    approvals = client.get("/approvals", headers=reviewer_headers).json()
    approval = next(row for row in approvals if row["proposal"]["id"] == run["proposal_id"])

    original_create = resolution_service.create_refund
    def accept_then_timeout(order_id, amount_minor, idempotency_key, *, bind=None):
        original_create(order_id, amount_minor, idempotency_key, bind=bind)
        raise TimeoutError("simulated lost response")
    monkeypatch.setattr(resolution_service, "create_refund", accept_then_timeout)

    first = client.post(f"/approvals/{approval['approval_id']}/decision", headers=reviewer_headers,
                        json={"decision": "approve"})
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending_reconciliation"

    monkeypatch.setattr(resolution_service, "create_refund", original_create)
    retry_rows = client.get("/approvals", headers=reviewer_headers).json()
    retry_approval = next(row for row in retry_rows if row["approval_id"] == approval["approval_id"])
    assert retry_approval["proposal"]["status"] == "pending_approval"
    second = client.post(f"/approvals/{approval['approval_id']}/decision", headers=reviewer_headers,
                         json={"decision": "approve"})
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "resolved"

    db = TestingSession()
    assert db.query(Payment).filter_by(order_id="order_a").one().refunded_amount_minor == 10000
    from app.db.models import MockProviderOperation, RefundOperation
    assert db.query(MockProviderOperation).filter_by(idempotency_key=f"refund:{run['proposal_id']}").count() == 1
    assert db.query(RefundOperation).filter_by(proposal_id=run["proposal_id"]).count() == 1
    db.close()


def test_order_status_escalates_when_shipping_record_is_missing():
    db = TestingSession()
    db.query(Shipment).filter_by(order_id="order_a").delete()
    db.commit(); db.close()
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers, json={
        "order_id": "order_a", "complaint": "Where is my order? Please share tracking status."
    }).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    proposal = client.get(f"/runs/{run['id']}/proposal", headers=headers).json()
    assert proposal["action_type"] == "escalate"
    assert "cannot be confirmed" in proposal["rationale"]


def test_order_status_escalates_when_shipping_record_is_stale():
    db = TestingSession()
    shipment = db.query(Shipment).filter_by(order_id="order_a").one()
    shipment.updated_at = datetime.now(timezone.utc) - timedelta(days=3)
    db.commit(); db.close()
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers, json={
        "order_id": "order_a", "complaint": "Where is my order? Please share tracking status."
    }).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    proposal = client.get(f"/runs/{run['id']}/proposal", headers=headers).json()
    assert proposal["action_type"] == "escalate"
    assert "stale" in proposal["rationale"]


def test_shipping_provider_failure_is_audited_and_does_not_invent_status(monkeypatch):
    import app.agents.workflow as workflow_module
    def fail_shipping(*args, **kwargs):
        raise ConnectionError("simulated carrier outage")
    monkeypatch.setattr(workflow_module, "get_shipment_status", fail_shipping)
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers, json={
        "order_id": "order_a", "complaint": "Where is my order? Please share tracking status."
    }).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    proposal = client.get(f"/runs/{run['id']}/proposal", headers=headers).json()
    events = client.get(f"/runs/{run['id']}/events", headers=headers).json()
    assert proposal["action_type"] == "escalate"
    assert any(event["event_type"] == "shipping_lookup_failed" for event in events)


def test_ticketing_timeout_after_accept_is_reconciled_by_reviewer_retry(monkeypatch):
    import app.services.ticketing_sync as ticketing_sync_module
    original_update = ticketing_sync_module.update_ticket_status
    def accept_then_timeout(ticket_id, status, idempotency_key, *, bind=None, **kwargs):
        return original_update(ticket_id, status, idempotency_key, bind=bind, timeout_after_accept=True)
    monkeypatch.setattr(ticketing_sync_module, "update_ticket_status", accept_then_timeout)

    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers, json={
        "order_id": "order_a", "complaint": "Where is my order? Please share tracking status."
    }).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    ticket_state = client.get(f"/tickets/{ticket['id']}", headers=headers).json()
    assert ticket_state["ticketing_sync_status"] == "pending"

    monkeypatch.setattr(ticketing_sync_module, "update_ticket_status", original_update)
    reviewer_headers = {"X-Reviewer-Token": "test-token", "X-Reviewer-ID": "reviewer-1"}
    retry = client.post(f"/runs/{run['id']}/ticket-sync", headers=reviewer_headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "synced"
    ticket_state = client.get(f"/tickets/{ticket['id']}", headers=headers).json()
    assert ticket_state["ticketing_sync_status"] == "synced"

    from app.db.models import MockTicketingOperation
    db = TestingSession()
    assert db.query(MockTicketingOperation).filter_by(ticket_id=ticket['id']).count() == 1
    db.close()


def test_ticket_complaint_rejects_whitespace_only_input():
    response = client.post("/tickets", headers={"X-Customer-ID": "cust_a"},
                           json={"order_id": "order_a", "complaint": "        "})
    assert response.status_code == 422


def test_health_checks_database_connectivity():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "connected"


def test_missing_policy_evidence_never_proposes_refund(monkeypatch):
    import app.agents.workflow as workflow_module
    monkeypatch.setattr(workflow_module, "retrieve_policies", lambda query, db=None: [])
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers, json={
        "order_id": "order_a", "complaint": "I returned my headphones and need a refund."
    }).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    proposal = client.get(f"/runs/{run['id']}/proposal", headers=headers).json()
    assert proposal["action_type"] == "escalate"
    assert proposal["amount_minor"] == 0
    assert "No applicable policy evidence" in proposal["rationale"]
    assert run["status"] == "needs_review"
    assert client.get("/approvals", headers={"X-Reviewer-Token": "test-token", "X-Reviewer-ID": "reviewer-1"}).json() == []


def test_damaged_item_clarification_then_evidence_escalation():
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers, json={
        "order_id": "order_a", "complaint": "My headphones arrived damaged and the casing is cracked. I need a refund."
    }).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    assert run["status"] == "needs_information"
    proposal = client.get(f"/runs/{run['id']}/proposal", headers=headers).json()
    assert proposal["action_type"] == "request_information"
    assert proposal["amount_minor"] == 0
    assert client.get("/approvals", headers={"X-Reviewer-Token": "test-token", "X-Reviewer-ID": "reviewer-1"}).json() == []

    followup = client.post(f"/runs/{run['id']}/messages", headers=headers, json={
        "message": "The left earcup is cracked. Photo reference: IMG-123."
    })
    assert followup.status_code == 201, followup.text
    next_run = followup.json()
    assert next_run["status"] == "needs_review"
    next_proposal = client.get(f"/runs/{next_run['id']}/proposal", headers=headers).json()
    assert next_proposal["action_type"] == "escalate"
    assert "evidence reference are recorded" in next_proposal["rationale"]
    messages = client.get(f"/tickets/{ticket['id']}/messages", headers=headers).json()
    assert len(messages) == 2
    assert messages[1]["message"].endswith("Photo reference: IMG-123.")
    db = TestingSession()
    assert db.query(TicketMessage).filter_by(ticket_id=ticket["id"]).count() == 2
    assert db.query(Payment).filter_by(order_id="order_a").one().refunded_amount_minor == 0
    db.close()


def test_followup_cannot_attach_another_customers_order():
    db = TestingSession()
    db.add(Customer(id="cust_b", name="Other Customer", email="other@test.invalid")); db.commit(); db.close()
    headers = {"X-Customer-ID": "cust_b"}
    ticket = client.post("/tickets", headers=headers, json={
        "complaint": "My product arrived damaged and the casing is cracked."
    }).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    assert run["status"] == "needs_information"
    response = client.post(f"/runs/{run['id']}/messages", headers=headers, json={
        "message": "Photo reference: IMG-999.", "order_id": "order_a"
    })
    assert response.status_code == 404


def test_damaged_item_workflow_has_a_followup_limit():
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers, json={
        "order_id": "order_a", "complaint": "My headphones arrived damaged and the casing is cracked."
    }).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    for attempt in range(4):
        response = client.post(f"/runs/{run['id']}/messages", headers=headers, json={
            "message": f"Additional damage details, follow-up {attempt + 1}; photo reference still being gathered."
        })
        assert response.status_code == 201, response.text
        run = response.json()
        assert run["status"] == "needs_information"
    blocked = client.post(f"/runs/{run['id']}/messages", headers=headers, json={
        "message": "One more follow-up."
    })
    assert blocked.status_code == 409
    assert "limit reached" in blocked.json()["detail"]


def test_workflow_rechecks_order_ownership_if_ticket_record_is_tampered():
    db = TestingSession()
    other = Customer(id="cust_b", name="Other Customer", email="other@test.invalid")
    foreign_order = Order(id="order_b", customer_id="cust_b", product_name="Private Product", status="delivered",
                          currency="INR", paid_amount_minor=999900)
    db.add_all([other, foreign_order]); db.commit(); db.close()
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers, json={
        "order_id": "order_a", "complaint": "I returned my headphones and need a refund."
    }).json()
    db = TestingSession()
    db.get(Ticket, ticket["id"]).order_id = "order_b"
    db.commit(); db.close()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    proposal = client.get(f"/runs/{run['id']}/proposal", headers=headers).json()
    assert proposal["action_type"] == "escalate"
    assert all(item.get("value") != "Private Product" for item in proposal["evidence"])
    events = client.get(f"/runs/{run['id']}/events", headers=headers).json()
    assert any(event["event_type"] == "order_ownership_denied" for event in events)


def test_post_approval_payload_change_invalidates_proposal_and_unblocks_case():
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers, json={
        "order_id": "order_a", "complaint": "I returned my headphones and need a refund."
    }).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    db = TestingSession()
    proposal = db.get(ActionProposal, run["proposal_id"])
    proposal.amount_minor += 1
    db.commit(); db.close()
    reviewer_headers = {"X-Reviewer-Token": "test-token", "X-Reviewer-ID": "reviewer-1"}
    approvals = client.get("/approvals", headers=reviewer_headers).json()
    approval = next(row for row in approvals if row["proposal"]["id"] == run["proposal_id"])
    response = client.post(f"/approvals/{approval['approval_id']}/decision", headers=reviewer_headers,
                           json={"decision": "approve"})
    assert response.status_code == 409
    db = TestingSession()
    assert db.get(Approval, approval["approval_id"]).decision == "invalidated"
    assert db.get(ActionProposal, run["proposal_id"]).status == "invalidated"
    assert db.get(WorkflowRun, run["id"]).status == "needs_review"
    assert db.get(Ticket, ticket["id"]).status == "needs_review"
    from app.db.models import MockProviderOperation
    assert db.query(MockProviderOperation).count() == 0
    db.close()
    new_run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers)
    assert new_run.status_code == 201


def test_failed_workflow_resumes_from_last_durable_checkpoint(monkeypatch):
    import app.agents.workflow as workflow_module
    original_retrieve = workflow_module.retrieve_policies
    calls = {"count": 0}
    def fail_once(query, db=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated policy service crash")
        return original_retrieve(query, db=db)
    monkeypatch.setattr(workflow_module, "retrieve_policies", fail_once)

    customer_headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=customer_headers, json={
        "order_id": "order_a", "complaint": "I returned my headphones and need a refund."
    }).json()
    failed = client.post(f"/tickets/{ticket['id']}/runs", headers=customer_headers).json()
    assert failed["status"] == "failed"
    assert failed["current_step"] == "investigate"
    assert failed["last_error"] == "RuntimeError"

    db = TestingSession()
    stored = db.get(WorkflowRun, failed["id"])
    assert stored.current_step == "investigate"
    assert "I returned my headphones" not in stored.state_json
    assert "customer_id" not in stored.state_json
    assert db.query(ActionProposal).filter_by(run_id=stored.id).count() == 0
    db.close()

    monkeypatch.setattr(workflow_module, "retrieve_policies", original_retrieve)
    reviewer_headers = {"X-Reviewer-Token": "test-token", "X-Reviewer-ID": "reviewer-1"}
    resumed_response = client.post(f"/runs/{failed['id']}/resume", headers=reviewer_headers)
    assert resumed_response.status_code == 200, resumed_response.text
    resumed = resumed_response.json()
    assert resumed["status"] == "awaiting_approval"
    assert resumed["current_step"] == "propose"
    assert resumed["last_error"] is None
    proposal = client.get(f"/runs/{failed['id']}/proposal", headers=customer_headers).json()
    assert proposal["action_type"] == "refund"

    db = TestingSession()
    assert db.query(ActionProposal).filter_by(run_id=failed["id"]).count() == 1
    events = db.query(AuditEvent).filter_by(run_id=failed["id"]).all()
    event_types = {event.event_type for event in events}
    assert {"checkpoint_saved", "workflow_failed", "workflow_resume_started"}.issubset(event_types)
    db.close()


def test_latest_run_endpoint_is_customer_scoped():
    db = TestingSession()
    db.add(Customer(id="cust_b", name="Other Customer", email="other@test.invalid")); db.commit(); db.close()
    headers = {"X-Customer-ID": "cust_a"}
    ticket = client.post("/tickets", headers=headers, json={
        "order_id": "order_a", "complaint": "Where is my order? Please share tracking status."
    }).json()
    run = client.post(f"/tickets/{ticket['id']}/runs", headers=headers).json()
    latest = client.get(f"/tickets/{ticket['id']}/runs/latest", headers=headers)
    assert latest.status_code == 200
    assert latest.json()["id"] == run["id"]
    assert client.get(f"/tickets/{ticket['id']}/runs/latest", headers={"X-Customer-ID": "cust_b"}).status_code == 404


def test_request_id_is_echoed_and_invalid_ids_are_replaced():
    valid = client.get("/health", headers={"X-Request-ID": "test-request_123"})
    assert valid.status_code == 200
    assert valid.headers["X-Request-ID"] == "test-request_123"
    invalid = client.get("/health", headers={"X-Request-ID": "contains spaces and secrets"})
    assert invalid.status_code == 200
    assert invalid.headers["X-Request-ID"] != "contains spaces and secrets"
    assert len(invalid.headers["X-Request-ID"]) == 32


def test_write_endpoint_rate_limit_returns_retry_after(monkeypatch):
    from app.core.config import settings
    from app.core.observability import reset_rate_limits_for_tests
    monkeypatch.setattr(settings, "rate_limit_requests", 1)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)
    reset_rate_limits_for_tests()
    headers = {"X-Customer-ID": "cust_a"}
    first = client.post("/tickets", headers=headers, json={"order_id":"order_a", "complaint":"I need help with an order."})
    second = client.post("/tickets", headers=headers, json={"order_id":"order_a", "complaint":"I need help with another order."})
    assert first.status_code == 201
    assert second.status_code == 429
    assert second.headers["Retry-After"]
    reset_rate_limits_for_tests()

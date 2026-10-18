from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from app.db.database import Base
from app.db.models import ActionProposal, Approval, Customer, MockProviderOperation, Order, Payment, RefundOperation, ReturnRecord, Ticket, WorkflowRun
from app.services.payment_provider import create_refund, get_refund, reset_provider_for_tests
from app.services.policies import retrieve_policies
from app.services.resolution import check_refund_eligibility, execute_approved_refund


def make_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_eligibility_requires_return_and_passed_inspection(tmp_path):
    db = make_db(tmp_path)
    order = Order(id="o1", customer_id="c1", product_name="Headphones", status="delivered", currency="INR", paid_amount_minor=10000)
    payment = Payment(id="p1", order_id="o1", status="paid", refunded_amount_minor=0)
    returned = ReturnRecord(id="r1", order_id="o1", status="received", inspection_status="passed")
    assert check_refund_eligibility(order, returned, payment)[0] is True
    returned.inspection_status = "failed"
    assert check_refund_eligibility(order, returned, payment)[0] is False
    returned.inspection_status = "passed"
    payment.refunded_amount_minor = 10000
    assert check_refund_eligibility(order, returned, payment)[0] is False
    db.close()


def test_mock_provider_idempotency_and_payload_mismatch(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'provider.db'}")
    Base.metadata.create_all(engine)
    reset_provider_for_tests(bind=engine)
    a = create_refund("o1", 5000, "key-1", bind=engine)
    b = create_refund("o1", 5000, "key-1", bind=engine)
    assert a == b
    try:
        create_refund("o1", 6000, "key-1", bind=engine)
        assert False, "different payload must fail"
    except ValueError as exc:
        assert "different payload" in str(exc)
    assert engine.connect().execute(__import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(MockProviderOperation)).scalar_one() == 1
    engine.dispose()


def test_policy_retrieval_returns_citations():
    hits = retrieve_policies("refund returned inspection passed eligibility")
    assert hits
    assert any("refund" in h["document"] for h in hits)
    assert all("section" in h and "text" in h and "score" in h for h in hits)


def test_execution_rechecks_approval_and_eligibility(tmp_path):
    db = make_db(tmp_path)
    customer = Customer(id="c1", name="Test Customer", email="test@example.test")
    order = Order(id="o1", customer_id="c1", product_name="Headphones", status="delivered", currency="INR", paid_amount_minor=10000)
    returned = ReturnRecord(id="r1", order_id="o1", status="received", inspection_status="passed")
    payment = Payment(id="p1", order_id="o1", status="paid", refunded_amount_minor=0)
    ticket = Ticket(id="t1", customer_id="c1", order_id="o1", complaint="I need a refund please.", category="refund", status="awaiting_approval")
    run = WorkflowRun(id="w1", ticket_id="t1", status="awaiting_approval")
    proposal = ActionProposal(id="pr1", run_id="w1", action_type="refund", amount_minor=10000, currency="INR",
        rationale="eligible", evidence_json="[]", policy_citations_json="[]", payload_hash="", status="pending_approval")
    from hashlib import sha256
    import json
    proposal.payload_hash = sha256(json.dumps({"run_id":"w1","customer_id":"c1","action_type":"refund","amount_minor":10000,"currency":"INR","order_id":"o1","rationale":"eligible","evidence_json":"[]","policy_citations_json":"[]"},sort_keys=True).encode()).hexdigest()
    approval = Approval(id="a1", proposal_id="pr1", reviewer_id="reviewer", decision="approved",
                        expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    db.add_all([customer, order, returned, payment, ticket, run, proposal, approval]); db.commit()
    result = execute_approved_refund(db, proposal, approval, "reviewer")
    assert result["status"] == "succeeded"
    assert payment.refunded_amount_minor == 10000
    assert payment.status == "refunded"
    replay = execute_approved_refund(db, proposal, approval, "reviewer")
    assert replay["idempotent_replay"] is True
    assert payment.refunded_amount_minor == 10000
    db.close()


def test_gemini_classifier_falls_back_without_api_key(monkeypatch):
    from app.agents.classifier import classify_complaint
    from app.core.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", "")
    category, source = classify_complaint("I returned the item and need a refund")
    assert category == "refund"
    assert source == "rules_fallback"


def test_provider_accept_then_application_rollback_is_reconciled_without_duplicate(tmp_path):
    db = make_db(tmp_path)
    customer = Customer(id="c1", name="Test Customer", email="test@example.test")
    order = Order(id="o1", customer_id="c1", product_name="Headphones", status="delivered", currency="INR", paid_amount_minor=10000)
    returned = ReturnRecord(id="r1", order_id="o1", status="received", inspection_status="passed")
    payment = Payment(id="p1", order_id="o1", status="paid", refunded_amount_minor=0)
    ticket = Ticket(id="t1", customer_id="c1", order_id="o1", complaint="I need a refund please.", category="refund", status="awaiting_approval")
    run = WorkflowRun(id="w1", ticket_id="t1", status="awaiting_approval")
    proposal = ActionProposal(id="pr1", run_id="w1", action_type="refund", amount_minor=10000, currency="INR",
        rationale="eligible", evidence_json="[]", policy_citations_json="[]", payload_hash="", status="pending_approval")
    from hashlib import sha256
    import json
    proposal.payload_hash = sha256(json.dumps({"run_id":"w1","customer_id":"c1","action_type":"refund","amount_minor":10000,"currency":"INR","order_id":"o1","rationale":"eligible","evidence_json":"[]","policy_citations_json":"[]"},sort_keys=True).encode()).hexdigest()
    approval = Approval(id="a1", proposal_id="pr1", reviewer_id="reviewer", decision="approved",
                        expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    db.add_all([customer, order, returned, payment, ticket, run, proposal, approval]); db.commit()

    first = execute_approved_refund(db, proposal, approval, "reviewer")
    assert first["status"] == "succeeded"
    db.rollback()  # Simulate the app process losing its transaction after provider acceptance.
    db.expire_all()

    proposal = db.get(ActionProposal, "pr1")
    approval = db.get(Approval, "a1")
    second = execute_approved_refund(db, proposal, approval, "reviewer")
    db.commit()

    assert second["status"] == "succeeded"
    assert db.get(Order, "o1").payment.refunded_amount_minor == 10000
    assert db.query(RefundOperation).filter_by(proposal_id="pr1").count() == 1
    assert db.query(MockProviderOperation).filter_by(idempotency_key="refund:pr1").count() == 1
    db.close()


def test_provider_timeout_after_acceptance_is_reconciled_by_lookup(tmp_path, monkeypatch):
    import app.services.resolution as resolution_service

    db = make_db(tmp_path)
    customer = Customer(id="c1", name="Test Customer", email="test@example.test")
    order = Order(id="o1", customer_id="c1", product_name="Headphones", status="delivered", currency="INR", paid_amount_minor=10000)
    returned = ReturnRecord(id="r1", order_id="o1", status="received", inspection_status="passed")
    payment = Payment(id="p1", order_id="o1", status="paid", refunded_amount_minor=0)
    ticket = Ticket(id="t1", customer_id="c1", order_id="o1", complaint="I need a refund please.", category="refund", status="awaiting_approval")
    run = WorkflowRun(id="w1", ticket_id="t1", status="awaiting_approval")
    from hashlib import sha256
    import json
    payload = {"run_id":"w1", "customer_id":"c1", "action_type":"refund", "amount_minor":10000, "currency":"INR", "order_id":"o1", "rationale":"eligible", "evidence_json":"[]", "policy_citations_json":"[]"}
    proposal = ActionProposal(id="pr1", run_id="w1", action_type="refund", amount_minor=10000, currency="INR",
        rationale="eligible", evidence_json="[]", policy_citations_json="[]",
        payload_hash=sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), status="pending_approval")
    approval = Approval(id="a1", proposal_id="pr1", reviewer_id="reviewer", decision="approved",
        decided_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    db.add_all([customer, order, returned, payment, ticket, run, proposal, approval]); db.commit()

    original_create = resolution_service.create_refund
    def accept_then_timeout(order_id, amount_minor, idempotency_key, *, bind=None):
        original_create(order_id, amount_minor, idempotency_key, bind=bind)
        raise TimeoutError("simulated lost provider response")
    monkeypatch.setattr(resolution_service, "create_refund", accept_then_timeout)

    first = execute_approved_refund(db, proposal, approval, "reviewer")
    assert first["status"] == "pending_reconciliation"
    assert db.get(Order, "o1").payment.refunded_amount_minor == 0
    assert db.query(RefundOperation).filter_by(proposal_id="pr1").one().status == "pending_reconciliation"

    monkeypatch.setattr(resolution_service, "create_refund", original_create)
    db.expire_all()
    second = execute_approved_refund(db, db.get(ActionProposal, "pr1"), db.get(Approval, "a1"), "reviewer")
    db.commit()
    assert second["status"] == "succeeded"
    assert db.get(Order, "o1").payment.refunded_amount_minor == 10000
    assert db.query(MockProviderOperation).filter_by(idempotency_key="refund:pr1").count() == 1
    db.close()


def test_changed_approved_proposal_payload_is_rejected(tmp_path):
    db = make_db(tmp_path)
    customer = Customer(id="c1", name="Test Customer", email="test@example.test")
    order = Order(id="o1", customer_id="c1", product_name="Headphones", status="delivered", currency="INR", paid_amount_minor=10000)
    returned = ReturnRecord(id="r1", order_id="o1", status="received", inspection_status="passed")
    payment = Payment(id="p1", order_id="o1", status="paid", refunded_amount_minor=0)
    ticket = Ticket(id="t1", customer_id="c1", order_id="o1", complaint="I need a refund please.", category="refund", status="awaiting_approval")
    run = WorkflowRun(id="w1", ticket_id="t1", status="awaiting_approval")
    proposal = ActionProposal(id="pr1", run_id="w1", action_type="refund", amount_minor=10000, currency="INR",
        rationale="eligible", evidence_json="[]", policy_citations_json="[]", payload_hash="invalid", status="pending_approval")
    approval = Approval(id="a1", proposal_id="pr1", reviewer_id="reviewer", decision="approved",
        decided_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    db.add_all([customer, order, returned, payment, ticket, run, proposal, approval]); db.commit()
    try:
        execute_approved_refund(db, proposal, approval, "reviewer")
        assert False, "A changed proposal payload must not execute."
    except ValueError as exc:
        assert "payload has changed" in str(exc)
    assert db.query(MockProviderOperation).count() == 0
    assert db.get(Order, "o1").payment.refunded_amount_minor == 0
    db.close()


def test_database_prevents_two_inflight_refunds_for_one_order(tmp_path):
    db = make_db(tmp_path)
    customer = Customer(id="c1", name="Test Customer", email="test@example.test")
    order = Order(id="o1", customer_id="c1", product_name="Headphones", status="delivered", currency="INR", paid_amount_minor=10000)
    ticket1 = Ticket(id="t1", customer_id="c1", order_id="o1", complaint="refund request one", category="refund")
    ticket2 = Ticket(id="t2", customer_id="c1", order_id="o1", complaint="refund request two", category="refund")
    run1 = WorkflowRun(id="w1", ticket_id="t1", status="awaiting_approval")
    run2 = WorkflowRun(id="w2", ticket_id="t2", status="awaiting_approval")
    proposal1 = ActionProposal(id="pr1", run_id="w1", action_type="refund", amount_minor=10000, currency="INR", rationale="eligible", payload_hash="a"*64)
    proposal2 = ActionProposal(id="pr2", run_id="w2", action_type="refund", amount_minor=10000, currency="INR", rationale="eligible", payload_hash="b"*64)
    db.add_all([customer, order, ticket1, ticket2, run1, run2, proposal1, proposal2]); db.commit()
    db.add(RefundOperation(id="op1", order_id="o1", proposal_id="pr1", amount_minor=10000, idempotency_key="refund:pr1", status="pending")); db.commit()
    db.add(RefundOperation(id="op2", order_id="o1", proposal_id="pr2", amount_minor=10000, idempotency_key="refund:pr2", status="pending"))
    try:
        db.commit()
        assert False, "The database must reject a second in-flight refund for the same order."
    except IntegrityError:
        db.rollback()
    assert db.query(RefundOperation).filter_by(order_id="o1", status="pending").count() == 1
    db.close()


def test_damage_claim_takes_precedence_over_refund_request():
    from app.agents.classifier import classify_complaint
    from app.core.config import settings
    settings.gemini_api_key = ""
    category, source = classify_complaint("My headphones arrived damaged and I need a refund.")
    assert category == "damaged_item"
    assert source == "rules_safety_override"


def test_refund_execution_rejects_order_owned_by_another_customer(tmp_path):
    db = make_db(tmp_path)
    customer = Customer(id="c1", name="Ticket Owner", email="owner@example.test")
    other = Customer(id="c2", name="Order Owner", email="other@example.test")
    order = Order(id="o1", customer_id="c2", product_name="Headphones", status="delivered", currency="INR", paid_amount_minor=10000)
    returned = ReturnRecord(id="r1", order_id="o1", status="received", inspection_status="passed")
    payment = Payment(id="p1", order_id="o1", status="paid", refunded_amount_minor=0)
    ticket = Ticket(id="t1", customer_id="c1", order_id="o1", complaint="I need a refund please.", category="refund")
    run = WorkflowRun(id="w1", ticket_id="t1", status="awaiting_approval")
    from hashlib import sha256
    import json
    payload = {"run_id":"w1", "customer_id":"c1", "action_type":"refund", "amount_minor":10000, "currency":"INR", "order_id":"o1", "rationale":"eligible", "evidence_json":"[]", "policy_citations_json":"[]"}
    proposal = ActionProposal(id="pr1", run_id="w1", action_type="refund", amount_minor=10000, currency="INR",
        rationale="eligible", evidence_json="[]", policy_citations_json="[]",
        payload_hash=sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), status="pending_approval")
    approval = Approval(id="a1", proposal_id="pr1", reviewer_id="reviewer", decision="approved",
        decided_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    db.add_all([customer, other, order, returned, payment, ticket, run, proposal, approval]); db.commit()
    try:
        execute_approved_refund(db, proposal, approval, "reviewer")
        assert False, "A ticket must not authorize a refund on another customer's order."
    except PermissionError as exc:
        assert "ownership" in str(exc)
    assert db.query(MockProviderOperation).count() == 0
    assert db.get(Order, "o1").payment.refunded_amount_minor == 0
    db.close()


def test_approved_evidence_and_policy_citations_are_bound_to_hash(tmp_path):
    db = make_db(tmp_path)
    customer = Customer(id="c1", name="Test Customer", email="test@example.test")
    order = Order(id="o1", customer_id="c1", product_name="Headphones", status="delivered", currency="INR", paid_amount_minor=10000)
    returned = ReturnRecord(id="r1", order_id="o1", status="received", inspection_status="passed")
    payment = Payment(id="p1", order_id="o1", status="paid", refunded_amount_minor=0)
    ticket = Ticket(id="t1", customer_id="c1", order_id="o1", complaint="I need a refund please.", category="refund")
    run = WorkflowRun(id="w1", ticket_id="t1", status="awaiting_approval")
    evidence = '[{"source":"returns","fact":"inspection_status","value":"passed"}]'
    citations = '[{"document":"refunds.md","section":"Refund eligibility"}]'
    rationale = "Return received and inspection passed."
    from hashlib import sha256
    import json
    payload = {"run_id":"w1", "customer_id":"c1", "action_type":"refund", "amount_minor":10000,
               "currency":"INR", "order_id":"o1", "rationale":rationale,
               "evidence_json":evidence, "policy_citations_json":citations}
    proposal = ActionProposal(id="pr1", run_id="w1", action_type="refund", amount_minor=10000, currency="INR",
        rationale=rationale, evidence_json=evidence, policy_citations_json=citations,
        payload_hash=sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), status="pending_approval")
    approval = Approval(id="a1", proposal_id="pr1", reviewer_id="reviewer", decision="approved",
        decided_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    db.add_all([customer, order, returned, payment, ticket, run, proposal, approval]); db.commit()
    proposal.evidence_json = "[]"
    try:
        execute_approved_refund(db, proposal, approval, "reviewer")
        assert False, "Changed evidence must invalidate the approved proposal hash."
    except ValueError as exc:
        assert "payload has changed" in str(exc)
    assert db.query(MockProviderOperation).count() == 0
    db.close()


def test_policy_ingestion_is_idempotent_and_retrieval_filters_effective_dates(tmp_path):
    from datetime import datetime, timezone
    from app.db.models import PolicyDocument, PolicyChunk
    from app.services.policies import ingest_policy_documents, retrieve_policies
    db = make_db(tmp_path)
    first = ingest_policy_documents(db)
    second = ingest_policy_documents(db)
    assert first == second
    assert db.query(PolicyDocument).count() == 2
    assert db.query(PolicyChunk).count() >= 5

    before_effective = retrieve_policies("refund eligibility return inspection", db=db,
        as_of=datetime(2025, 12, 31, tzinfo=timezone.utc))
    active = retrieve_policies("refund eligibility return inspection", db=db,
        as_of=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert before_effective == []
    assert active
    assert all(item["version"] == "1.0.0" and item["effective_from"].startswith("2026-01-01") for item in active)
    db.close()


def test_policy_source_cannot_change_without_version_bump(tmp_path, monkeypatch):
    import app.services.policies as policy_service
    from app.db.models import PolicyDocument
    db = make_db(tmp_path)
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    policy_file = policy_dir / "sample.md"
    policy_file.write_text("---\nversion: 1.0.0\neffective_from: 2026-01-01\neffective_to: null\n---\n# Sample policy\n\n## Refunds\nRefunds require approval.\n")
    monkeypatch.setattr(policy_service, "POLICY_DIR", policy_dir)
    policy_service.ingest_policy_documents(db)
    assert db.query(PolicyDocument).count() == 1
    policy_file.write_text("---\nversion: 1.0.0\neffective_from: 2026-01-01\neffective_to: null\n---\n# Sample policy\n\n## Refunds\nRefunds now require two approvals.\n")
    try:
        policy_service.ingest_policy_documents(db)
        assert False, "A policy document must not change under an existing version." 
    except ValueError as exc:
        assert "version bump" in str(exc)
    db.close()


def test_overlapping_active_policy_versions_are_flagged_and_escalated(tmp_path):
    import hashlib
    from app.db.models import PolicyDocument, PolicyChunk, Ticket, WorkflowRun
    from app.services.policies import ingest_policy_documents, retrieve_policies
    from app.services.resolution import create_proposal
    db = make_db(tmp_path)
    ingest_policy_documents(db)
    original = db.query(PolicyDocument).filter_by(source_uri="policies/refunds.md").one()
    future = PolicyDocument(id=hashlib.sha256(b"policies/refunds.md:2.0.0").hexdigest()[:40],
        title=original.title, version="2.0.0", effective_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        effective_to=None, source_uri=original.source_uri, content_hash="f"*64)
    db.add(future); db.flush()
    db.add(PolicyChunk(id=hashlib.sha256(b"future-policy-chunk").hexdigest()[:40], document_id=future.id,
        section="Refund eligibility", content="Refunds now require two inspections and reviewer approval.",
        chunk_index=0, metadata_json='{"version":"2.0.0"}'))
    customer = Customer(id="c1", name="Test Customer", email="test@example.test")
    ticket = Ticket(id="t1", customer_id="c1", complaint="I need a refund", category="refund")
    run = WorkflowRun(id="w1", ticket_id="t1", status="running")
    db.add_all([customer, ticket, run]); db.commit()
    citations = retrieve_policies("refund eligibility inspection", db=db)
    assert any(item.get("policy_conflict") for item in citations)
    proposal = create_proposal(db, "w1", None, None, None, [], citations, category="refund")
    assert proposal.action_type == "escalate"
    assert "Conflicting active versions" in proposal.rationale
    db.close()


def test_policy_version_transition_closes_old_version_and_activates_new_one(tmp_path, monkeypatch):
    import app.services.policies as policy_service
    from app.db.models import PolicyDocument
    from app.services.policies import ingest_policy_documents, retrieve_policies
    db = make_db(tmp_path)
    policy_dir = tmp_path / "versioned-policies"
    policy_dir.mkdir()
    v1 = policy_dir / "refunds-v1.md"
    v1.write_text("---\nversion: 1.0.0\neffective_from: 2026-01-01\neffective_to: null\n---\n# Refund policy\n\n## Refund eligibility\nRefunds require return receipt and inspection approval.\n")
    monkeypatch.setattr(policy_service, "POLICY_DIR", policy_dir)
    ingest_policy_documents(db)
    v1.write_text("---\nversion: 1.0.0\neffective_from: 2026-01-01\neffective_to: 2026-12-31\n---\n# Refund policy\n\n## Refund eligibility\nRefunds require return receipt and inspection approval.\n")
    v2 = policy_dir / "refunds-v2.md"
    v2.write_text("---\nversion: 2.0.0\neffective_from: 2027-01-01\neffective_to: null\n---\n# Refund policy\n\n## Refund eligibility\nRefunds require return receipt, inspection approval, and a signed customer declaration.\n")
    ingest_policy_documents(db)
    before = retrieve_policies("refunds return receipt inspection", db=db,
        as_of=datetime(2026, 6, 1, tzinfo=timezone.utc))
    after = retrieve_policies("refunds return receipt inspection", db=db,
        as_of=datetime(2027, 6, 1, tzinfo=timezone.utc))
    assert before and all(item["version"] == "1.0.0" for item in before)
    assert after and all(item["version"] == "2.0.0" for item in after)
    assert not any(item.get("policy_conflict") for item in before + after)
    assert db.query(PolicyDocument).count() == 2
    db.close()


def test_refund_eligibility_requires_settled_payment_and_delivered_order():
    from app.services.resolution import check_refund_eligibility
    order = Order(id="o1", customer_id="c1", product_name="Headphones", status="delivered", currency="INR", paid_amount_minor=10000)
    returned = ReturnRecord(id="r1", order_id="o1", status="received", inspection_status="passed")
    payment = Payment(id="p1", order_id="o1", status="failed", refunded_amount_minor=0)
    eligible, reason = check_refund_eligibility(order, returned, payment)
    assert not eligible and "not eligible" in reason
    payment.status = "paid"
    order.status = "shipped"
    eligible, reason = check_refund_eligibility(order, returned, payment)
    assert not eligible and "delivery/return facts" in reason


def test_payment_provider_failure_and_timeout_environment_controls(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'payment-provider.db'}")
    Base.metadata.create_all(engine)
    reset_provider_for_tests(bind=engine)
    monkeypatch.setenv("MOCK_PAYMENT_FAIL", "1")
    with pytest.raises(ConnectionError):
        create_refund("o1", 5000, "payment-key", bind=engine)
    assert get_refund("payment-key", bind=engine) is None

    monkeypatch.setenv("MOCK_PAYMENT_FAIL", "0")
    monkeypatch.setenv("MOCK_PAYMENT_TIMEOUT_AFTER_ACCEPT", "1")
    with pytest.raises(TimeoutError):
        create_refund("o1", 5000, "payment-key", bind=engine)
    existing = get_refund("payment-key", bind=engine)
    assert existing is not None and existing["status"] == "succeeded"

    monkeypatch.setenv("MOCK_PAYMENT_TIMEOUT_AFTER_ACCEPT", "0")
    replay = create_refund("o1", 5000, "payment-key", bind=engine)
    assert replay["provider_reference"] == existing["provider_reference"]
    engine.dispose()


def test_faulty_item_claim_overrides_refund_intent():
    from app.agents.classifier import classify_complaint
    from app.core.config import settings
    settings.gemini_api_key = ""
    category, source = classify_complaint("My speaker is faulty and does not work; I need a refund.")
    assert category == "damaged_item"
    assert source == "rules_safety_override"


def test_gemini_structured_classification_path_uses_parsed_output(monkeypatch):
    import sys
    import types as pytypes
    from types import SimpleNamespace
    from app.agents.classifier import classify_complaint
    from app.core.config import settings

    class FakeModels:
        def generate_content(self, **kwargs):
            assert kwargs["config"]["response_mime_type"] == "application/json"
            return SimpleNamespace(parsed={"category": "refund", "rationale": "Return request"}, text='{"category":"refund"}')

    class FakeClient:
        def __init__(self, api_key):
            assert api_key == "test-key"
            self.models = FakeModels()

    fake_genai = pytypes.ModuleType("google.genai")
    fake_genai.__path__ = []
    fake_genai.Client = FakeClient
    fake_types = pytypes.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **kwargs: kwargs
    fake_genai.types = fake_types
    fake_google = pytypes.ModuleType("google")
    fake_google.__path__ = []
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    category, source = classify_complaint("I returned my headphones and need a refund.")
    assert category == "refund"
    assert source == "gemini_structured"

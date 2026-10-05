import hashlib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import payhere
from app.db.models import Order, Product, PaymentReview
from app.main import app
from app.tools import orders
from scripts.seed import seed_database

SECRET = "test-secret"


def signed(code=2, **overrides):
    fields = dict(merchant_id="test-merchant", order_id="ORD-0003", payment_id="test-payment",
                  payhere_amount="5430.00", payhere_currency="LKR", status_code=str(code))
    fields.update(overrides)
    # Independent transcription of the published formula, not production verify.
    inner = hashlib.md5(SECRET.encode()).hexdigest().upper()
    raw = ''.join(fields[k] for k in ("merchant_id", "order_id", "payhere_amount", "payhere_currency", "status_code")) + inner
    fields["md5sig"] = hashlib.md5(raw.encode()).hexdigest().upper()
    return fields


@pytest.fixture
def payment_db(db_engine, monkeypatch):
    seed_database(db_engine)
    monkeypatch.setattr(payhere, "get_order_engine", lambda: db_engine)
    monkeypatch.setattr(orders, "get_order_engine", lambda: db_engine)
    monkeypatch.setenv("PAYHERE_MERCHANT_ID", "test-merchant")
    monkeypatch.setenv("PAYHERE_MERCHANT_SECRET", SECRET)
    monkeypatch.setenv("PAYHERE_PUBLIC_BASE_URL", "https://example.com")
    monkeypatch.setenv("PAYHERE_MODE", "sandbox")
    return db_engine


def snapshot(engine):
    with Session(engine) as session:
        return {model.__tablename__: [tuple(row) for row in session.execute(select(model.__table__).order_by(*model.__table__.primary_key))]
                for model in (Order, Product, PaymentReview)}


def test_exact_hash_formula():
    inner = hashlib.md5(SECRET.encode()).hexdigest().upper()
    expected = hashlib.md5(("test-merchantORD-00035430.00LKR" + inner).encode()).hexdigest().upper()
    assert payhere.make_hash("test-merchant", "ORD-0003", Decimal("5430"), SECRET) == expected
    assert payhere.verify(signed(), SECRET)
    assert not payhere.verify({}, SECRET)


def test_case_11_failed_order_retry_link(payment_db):
    status = orders.get_order_status.invoke({"order_id": "ORD-0002"})
    assert status["status"] == "PAYMENT_FAILED"
    before = snapshot(payment_db)
    link = orders.get_payment_link.invoke({"order_id": "ORD-0002"})
    assert link["url"].startswith("https://example.com/payments/payhere?")
    assert snapshot(payment_db) == before


def test_case_12_paid_and_duplicate_notification(payment_db):
    client = TestClient(app)
    assert client.post("/webhooks/payhere", data=signed()).status_code == 200
    with Session(payment_db) as session:
        assert session.get(Order, "ORD-0003").status == "PAID"
        assert session.get(Order, "ORD-0003").payhere_payment_id == "test-payment"
        assert session.get(Product, "PRD-35").stock == 143
        assert session.get(Product, "PRD-30").stock == 29
    after = snapshot(payment_db)
    for code in (2, 0, -1, -2):
        assert client.post("/webhooks/payhere", data=signed(code)).status_code == 200
        assert snapshot(payment_db) == after


def test_case_13_forged_signature_unchanged(payment_db, caplog):
    before = snapshot(payment_db)
    fields = signed()
    fields["md5sig"] = "0" * 32
    assert TestClient(app).post("/webhooks/payhere", data=fields).status_code == 400
    assert snapshot(payment_db) == before
    assert "invalid signature" in caplog.text


@pytest.mark.parametrize("code,status", [(0, "PENDING"), (-1, "PAYMENT_FAILED"), (-2, "PAYMENT_FAILED")])
def test_other_statuses_do_not_reduce_stock(payment_db, code, status):
    before = snapshot(payment_db)["products"]
    assert TestClient(app).post("/webhooks/payhere", data=signed(code)).status_code == 200
    assert orders.get_order_status.invoke({"order_id": "ORD-0003"})["status"] == status
    assert snapshot(payment_db)["products"] == before


def test_chargeback_durable_flag_without_status_change(payment_db):
    before = snapshot(payment_db)
    for _ in range(2):
        assert TestClient(app).post("/webhooks/payhere", data=signed(-3)).json()["status"] == "review_required"
    after = snapshot(payment_db)
    assert after["orders"] == before["orders"]
    assert after["products"] == before["products"]
    assert len(after["payment_reviews"]) == 1


@pytest.mark.parametrize("overrides", [
    {"payhere_amount": "1.00"}, {"payhere_currency": "USD"}, {"merchant_id": "another"},
    {"payhere_amount": "NaN"}, {"status_code": "99"}, {"payment_id": ""},
])
def test_signed_invalid_values_leave_order_unchanged(payment_db, overrides):
    before = snapshot(payment_db)
    assert TestClient(app).post("/webhooks/payhere", data=signed(**overrides)).status_code == 400
    assert snapshot(payment_db) == before


def test_concurrent_success_deducts_once(payment_db):
    def send(_):
        with TestClient(app) as client:
            return client.post("/webhooks/payhere", data=signed()).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(send, range(2))) == [200, 200]
    with Session(payment_db) as session:
        assert session.get(Product, "PRD-35").stock == 143


def test_short_stock_rolls_back_order_and_all_products(payment_db):
    with Session(payment_db) as session, session.begin():
        session.get(Product, "PRD-35").stock = 1
    before = snapshot(payment_db)
    assert TestClient(app).post("/webhooks/payhere", data=signed()).status_code == 409
    assert snapshot(payment_db) == before


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}
        self.action = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input":
            self.fields[attrs["name"]] = attrs["value"]
        if tag == "form":
            self.action = attrs["action"]


def test_signed_form_and_return_page(payment_db):
    client = TestClient(app)
    before = snapshot(payment_db)
    link = orders.get_payment_link.invoke({"order_id": "ORD-0002"})
    parts = urlsplit(link["url"])
    response = client.get(parts.path + "?" + parts.query)
    assert response.status_code == 200
    assert response.headers["referrer-policy"] == "strict-origin"
    assert response.headers["cache-control"] == "no-store"
    parser = FormParser()
    parser.feed(response.text)
    assert parser.action == "https://sandbox.payhere.lk/pay/checkout"
    fields = parser.fields
    assert fields["amount"] == "26625.00" and fields["currency"] == "LKR"
    assert fields["city"] == "Jaffna" and fields["custom_1"] == "sess_003"
    assert fields["hash"] == payhere.make_hash("test-merchant", "ORD-0002", Decimal("26625.00"), SECRET)
    assert fields["notify_url"] == "https://example.com/webhooks/payhere"
    assert SECRET not in response.text
    assert client.get("/payments/payhere", params={"order_id": "ORD-0002", "expires": link["expires"], "token": "forged"}).status_code == 403
    assert client.get("/payments/payhere/return?status_code=2").status_code == 200
    assert client.get("/payments/payhere/cancel").status_code == 200
    assert snapshot(payment_db) == before

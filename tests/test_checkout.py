from decimal import Decimal

import pytest
from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Cart, Order, Product
from app.graph.checkout import build_checkout_graph
from app.tools import cart, orders
from scripts.seed import seed_database

CUSTOMER = dict(session_id="checkout-session", customer_name="Test Customer",
                phone="0771234567", email="test@example.com", address="12 Test Road, Colombo")
CONFIG = {"configurable": {"thread_id": CUSTOMER["session_id"]}}


@pytest.fixture
def checkout_db(db_engine, monkeypatch):
    monkeypatch.setenv("PAYHERE_MERCHANT_ID", "test-merchant")
    monkeypatch.setenv("PAYHERE_MERCHANT_SECRET", "test-secret")
    monkeypatch.setenv("PAYHERE_PUBLIC_BASE_URL", "https://example.com")
    seed_database(db_engine)
    monkeypatch.setattr(cart, "get_cart_engine", lambda: db_engine)
    monkeypatch.setattr(orders, "get_order_engine", lambda: db_engine)
    for product_id, quantity in [("PRD-06", 2), ("PRD-13", 1)]:
        cart.add_to_cart.invoke(dict(session_id=CUSTOMER["session_id"], product_id=product_id, quantity=quantity))
    return db_engine


def order_count(engine):
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(Order))


def test_pause_restart_resume_and_replay(checkout_db, checkpoint_factory):
    with checkpoint_factory() as saver:
        graph = build_checkout_graph(saver)
        paused = graph.invoke(CUSTOMER, CONFIG)
        snapshot = paused["__interrupt__"][0].value["cart"]
        assert snapshot["total"] == "23905.00"
        assert len(snapshot["items"]) == 2
        assert order_count(checkout_db) == 3
    # New connection and graph: resume the persisted pending interrupt.
    with checkpoint_factory() as saver:
        graph = build_checkout_graph(saver)
        assert graph.get_state(CONFIG).next == ("confirm",)
        result = graph.invoke(Command(resume=True), CONFIG)
        assert result["order"]["status"] == "PENDING"
        assert result["order"]["total"] == "23905.00"
        assert result["payment"]["url"].startswith("https://example.com/payments/payhere?")
        assert graph.get_state(CONFIG).next == ()
    assert order_count(checkout_db) == 4
    with Session(checkout_db) as session:
        assert session.get(Cart, snapshot["id"]).status == "CONVERTED"
        assert session.get(Product, "PRD-06").stock == 23
    replay = orders.create_order(orders.PlaceOrderInput(**CUSTOMER), snapshot)
    assert replay == result["order"]
    assert order_count(checkout_db) == 4


@pytest.mark.parametrize("answer", ["yes", "true", 1, {"confirmed": True}])
def test_only_boolean_true_proceeds(checkout_db, pg_checkpointer, answer):
    graph = build_checkout_graph(pg_checkpointer)
    graph.invoke(CUSTOMER, CONFIG)
    result = graph.invoke(Command(resume=answer), CONFIG)
    assert result["__interrupt__"]
    assert order_count(checkout_db) == 3
    graph.invoke(Command(resume=True), CONFIG)
    assert order_count(checkout_db) == 4


def test_cancel(checkout_db, pg_checkpointer):
    graph = build_checkout_graph(pg_checkpointer)
    graph.invoke(CUSTOMER, CONFIG)
    result = graph.invoke(Command(resume=False), CONFIG)
    assert result["confirmed"] is False
    assert result["order"] is None
    assert order_count(checkout_db) == 3


def test_price_change_requires_new_confirmation(checkout_db, pg_checkpointer):
    graph = build_checkout_graph(pg_checkpointer)
    graph.invoke(CUSTOMER, CONFIG)
    with Session(checkout_db) as session, session.begin():
        session.get(Product, "PRD-06").sale_price = Decimal("100.00")
    result = graph.invoke(Command(resume=True), CONFIG)
    assert result["__interrupt__"][0].value["cart"]["total"] == "8615.00"
    assert order_count(checkout_db) == 3
    final = graph.invoke(Command(resume=True), CONFIG)
    assert final["order"]["total"] == "8615.00"


def test_stock_change_refuses_order(checkout_db, pg_checkpointer):
    graph = build_checkout_graph(pg_checkpointer)
    graph.invoke(CUSTOMER, CONFIG)
    with Session(checkout_db) as session, session.begin():
        session.get(Product, "PRD-06").stock = 1
    result = graph.invoke(Command(resume=True), CONFIG)
    assert result["error"]["error"] == "insufficient_stock"
    assert order_count(checkout_db) == 3
    assert cart.view_cart.invoke({"session_id": CUSTOMER["session_id"]})["status"] == "OPEN"


def test_empty_cart_and_thread_mismatch(checkout_db, pg_checkpointer):
    graph = build_checkout_graph(pg_checkpointer)
    with pytest.raises(ValueError, match="thread_id"):
        graph.invoke(CUSTOMER, {"configurable": {"thread_id": "other"}})
    state = graph.invoke({**CUSTOMER, "session_id": "empty"}, {"configurable": {"thread_id": "empty"}})
    assert state["error"]["error"] == "empty_cart"
    assert order_count(checkout_db) == 3


def test_raw_order_tools_refresh_prices_and_lookup(checkout_db):
    with Session(checkout_db) as session, session.begin():
        session.get(Product, "PRD-06").sale_price = Decimal("100.25")
    result = orders.place_order.invoke(CUSTOMER)
    assert result["total"] == "8615.50"
    assert orders.get_order_status.invoke({"order_id": result["order_id"]}) == result
    assert orders.get_order_status.invoke({"order_id": "ORD-0002"})["status"] == "PAYMENT_FAILED"
    assert orders.get_payment_link.invoke({"order_id": "ORD-0002"})["url"]
    assert orders.get_payment_link.invoke({"order_id": "ORD-0001"})["error"] == "already_paid"
    assert orders.get_order_status.invoke({"order_id": "missing"})["error"] == "order_not_found"
    assert orders.get_payment_link.invoke({"order_id": "missing"})["error"] == "order_not_found"

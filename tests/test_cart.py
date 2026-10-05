from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Cart, CartItem, Product
from app.tools import cart
from scripts.seed import seed_database


@pytest.fixture
def cart_db(db_engine, monkeypatch):
    seed_database(db_engine)
    monkeypatch.setattr(cart, "get_cart_engine", lambda: db_engine)
    return db_engine


def add(product_id, quantity, session_id="new-session"):
    return cart.add_to_cart.invoke(dict(session_id=session_id, product_id=product_id, quantity=quantity))


def snapshot(engine):
    with Session(engine) as session:
        return {
            model.__tablename__: [tuple(row) for row in session.execute(select(model.__table__).order_by(*model.__table__.primary_key))]
            for model in (Cart, CartItem, Product)
        }


def test_case_8_add_cameras_and_switch(cart_db):
    add("PRD-06", 2)
    result = add("PRD-13", 1)
    assert result["total"] == "23905.00"
    assert [(i["product_id"], i["quantity"], i["unit_price"]) for i in result["items"]] == [("PRD-06", 2, "7745.00"), ("PRD-13", 1, "8415.00")]
    with Session(cart_db) as session:
        assert session.get(Cart, result["id"]).total == Decimal("23905.00")
        assert session.get(Product, "PRD-06").stock == 23


def test_case_9_refuse_50_and_preserve_cart(cart_db):
    add("PRD-06", 2)
    add("PRD-13", 1)
    before = snapshot(cart_db)
    result = add("PRD-06", 50)
    assert result == {"error": "insufficient_stock", "product_id": "PRD-06", "requested_quantity": 52, "available_stock": 23}
    assert snapshot(cart_db) == before


def test_case_10_remove_switch(cart_db):
    add("PRD-06", 2)
    add("PRD-13", 1)
    result = cart.remove_from_cart.invoke({"session_id": "new-session", "product_id": "PRD-13"})
    assert result["total"] == "15490.00"
    assert [i["product_id"] for i in result["items"]] == ["PRD-06"]


def test_upsert_updates_zero_and_cumulative_stock(cart_db):
    add("PRD-06", 20)
    assert add("PRD-06", 3)["items"][0]["quantity"] == 23
    assert add("PRD-06", 1)["error"] == "insufficient_stock"
    args = {"session_id": "new-session", "product_id": "PRD-06"}
    assert cart.update_cart_item.invoke({**args, "quantity": 2})["total"] == "15490.00"
    before = snapshot(cart_db)
    assert cart.update_cart_item.invoke({**args, "quantity": 24})["error"] == "insufficient_stock"
    assert snapshot(cart_db) == before
    result = cart.update_cart_item.invoke({**args, "quantity": 0})
    assert result["items"] == [] and result["total"] == "0.00"


def test_live_prices_stock_and_refusal_atomicity(cart_db):
    result = add("PRD-06", 2)
    with Session(cart_db) as session, session.begin():
        product = session.get(Product, "PRD-06")
        product.sale_price = Decimal("100.25")
        product.stock = 1
    before = snapshot(cart_db)
    assert add("PRD-06", 1)["available_stock"] == 1
    assert snapshot(cart_db) == before
    viewed = cart.view_cart.invoke({"session_id": "new-session"})
    assert viewed["total"] == "200.50"
    assert viewed["items"][0]["available"] is False
    assert viewed["items"][0]["stock_label"] == "Only 1 left"
    with Session(cart_db) as session:
        assert session.get(Cart, result["id"]).total == Decimal("200.50")
    changed = cart.update_cart_item.invoke({"session_id": "new-session", "product_id": "PRD-06", "quantity": 1})
    assert changed["total"] == "100.25" and changed["items"][0]["available"]


def test_empty_missing_and_closed_carts(cart_db):
    before = snapshot(cart_db)
    assert cart.view_cart.invoke({"session_id": "absent"})["items"] == []
    assert cart.remove_from_cart.invoke({"session_id": "absent", "product_id": "missing"})["total"] == "0.00"
    assert add("missing", 1)["error"] == "product_not_found"
    assert add("PRD-14", 1)["error"] == "insufficient_stock"
    assert cart.update_cart_item.invoke({"session_id": "absent", "product_id": "PRD-06", "quantity": 1})["error"] == "cart_item_not_found"
    assert snapshot(cart_db) == before
    result = add("PRD-06", 1, "sess_001")
    assert result["id"] != "CART-01"
    with Session(cart_db) as session:
        assert session.get(Cart, "CART-01").status == "CONVERTED"
        assert session.get(Cart, "CART-01").total == Decimal("23905.00")
    assert cart.view_cart.invoke({"session_id": "sess_002"})["total"] == "16705.00"


def test_concurrent_first_adds_create_one_cart_without_lost_updates(cart_db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: add("PRD-06", 1, "concurrent"), range(2)))
    assert len({r["id"] for r in results}) == 1
    assert cart.view_cart.invoke({"session_id": "concurrent"})["items"][0]["quantity"] == 2
    with Session(cart_db) as session:
        assert session.scalar(select(func.count()).select_from(Cart).where(Cart.session_id == "concurrent")) == 1


@pytest.mark.parametrize("tool,quantity", [(cart.add_to_cart, 0), (cart.add_to_cart, -1), (cart.add_to_cart, 1.5), (cart.add_to_cart, True), (cart.update_cart_item, -1)])
def test_invalid_quantities(tool, quantity):
    with pytest.raises(ValidationError):
        tool.invoke({"session_id": "session", "product_id": "PRD-06", "quantity": quantity})

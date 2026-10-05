import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Cart, CartItem, Category, Order, Product, stock_label
from app.db.schema import create_schema
from scripts.seed import DEFAULT_SOURCE, seed_database


def test_seed_matches_every_source_field(db_engine):
    source = json.loads(DEFAULT_SOURCE.read_text(encoding="utf-8"), parse_float=Decimal)
    assert seed_database(db_engine) == {"categories": 14, "products": 38, "carts": 5, "orders": 3}
    with Session(db_engine) as session:
        for key, model, count in (("categories", Category, 14), ("products", Product, 38), ("carts", Cart, 5), ("orders", Order, 3)):
            assert session.scalar(select(func.count()).select_from(model)) == count
            for record in source[key]:
                row = session.get(model, record["id"])
                for field, expected in record.items():
                    if field == "items":
                        actual = [{k: getattr(item, k) for k in ("product_id", "quantity", "unit_price")} for item in row.items]
                        assert sorted(actual, key=lambda i: i["product_id"]) == sorted(expected, key=lambda i: i["product_id"])
                    else:
                        assert getattr(row, field) == expected
        assert session.scalar(select(func.count()).select_from(CartItem)) == 8
        for cart in session.scalars(select(Cart)):
            assert cart.total == sum(item.quantity * item.unit_price for item in cart.items)
        assert session.get(Product, "PRD-01").selling_price == Decimal("26625.00")
        assert session.get(Product, "PRD-14").stock_label == "Out of stock"
        assert session.get(Product, "PRD-10").stock_label == "Only 2 left"
        assert session.get(Product, "PRD-04").discount_percent == 10
        assert session.get(Product, "PRD-05").discount_percent == 24
        assert session.get(Product, "PRD-06").selling_price == Decimal("7745.00")
        assert session.get(Product, "PRD-06").discount_percent == 0


def test_repeated_seed_preserves_existing_data(db_engine):
    seed_database(db_engine)
    with Session(db_engine) as session, session.begin():
        session.get(Product, "PRD-01").stock = 3
    create_schema(db_engine)
    assert seed_database(db_engine) == {"categories": 0, "products": 0, "carts": 0, "orders": 0}
    with Session(db_engine) as session:
        assert session.get(Product, "PRD-01").stock == 3
        assert session.scalar(select(func.count()).select_from(CartItem)) == 8


def test_invalid_seed_rolls_back_all_records(db_engine, tmp_path):
    data = json.loads(DEFAULT_SOURCE.read_text(encoding="utf-8"))
    data["orders"][-1]["cart_id"] = "MISSING-CART"
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(IntegrityError):
        seed_database(db_engine, source)
    with Session(db_engine) as session:
        for model in (Category, Product, Cart, CartItem, Order):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("stock,expected", [(0, "Out of stock"), (1, "Only 1 left"), (2, "Only 2 left"), (4, "Only 4 left"), (5, "In stock"), (145, "In stock")])
def test_stock_label_boundaries(stock, expected):
    assert stock_label(stock) == expected


@pytest.mark.parametrize("sale,selling,discount", [(None, "100.00", 0), ("0.00", "0.00", 0), ("75.50", "75.50", 24), ("74.50", "74.50", 26)])
def test_price_null_zero_and_rounding(sale, selling, discount):
    product = Product(price=Decimal("100.00"), sale_price=None if sale is None else Decimal(sale))
    assert product.selling_price == Decimal(selling)
    assert product.discount_percent == discount

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models import Category, Product
from app.tools import catalog
from scripts.seed import DEFAULT_SOURCE, seed_database


@pytest.fixture
def seeded_catalog(db_engine, monkeypatch):
    seed_database(db_engine)
    monkeypatch.setattr(catalog, "get_catalog_engine", lambda: db_engine)
    return db_engine


def test_case_1_categories(seeded_catalog):
    rows = catalog.list_categories.invoke({})
    source = json.loads(DEFAULT_SOURCE.read_text())
    assert rows == [dict(category, product_count=sum(p["category_id"] == category["id"] for p in source["products"])) for category in source["categories"]]
    assert len(rows) == 14
    assert sum(row["product_count"] for row in rows) == 38


def test_case_2_cli_phones_under_5000(seeded_catalog):
    rows = catalog.search_products.invoke({"query": "CLI", "category_id": "CAT-04", "max_price": 5000})
    assert [(p["id"], p["name"], p["selling_price"]) for p in rows] == [("PRD-08", "Emark HCD-211N CLI Phone", "4380.00")]


def test_case_3_4g_router_offers(seeded_catalog):
    rows = catalog.search_products.invoke({"category_id": "CAT-02", "on_sale": True})
    assert [(p["id"], p["discount_percent"], p["stock_label"]) for p in rows] == [
        ("PRD-04", 10, "In stock"), ("PRD-05", 24, "In stock"), ("PRD-21", 21, "Out of stock")]


def test_case_4_cheapest_wifi_extender(seeded_catalog):
    rows = catalog.search_products.invoke({"query": "extender", "category_id": "CAT-01", "sort": "price_asc"})
    assert [(p["id"], p["selling_price"]) for p in rows[:2]] == [("PRD-19", "8990.00"), ("PRD-20", "9255.00")]


def test_case_5_siyol_out_of_stock(seeded_catalog):
    assert catalog.check_stock.invoke({"product_id": "PRD-21"}) == {
        "product_id": "PRD-21", "name": "SIYOL Pocket Mi-Fi Router", "stock": 0, "label": "Out of stock"}


def test_case_6_alcatel_low_stock(seeded_catalog):
    assert catalog.check_stock.invoke({"product_id": "PRD-10"}) == {
        "product_id": "PRD-10", "name": "ALCATEL S280 Cordless Phone", "stock": 2, "label": "Only 2 left"}


def test_case_7_compare_tapo(seeded_catalog):
    result = catalog.compare_products.invoke({"product_ids": ["PRD-06", "PRD-07"]})
    assert result["product_ids"] == ["PRD-06", "PRD-07"]
    assert result["fields"]["selling_price"] == ["7745.00", "17595.00"]
    assert "indoor" in result["fields"]["description"][0]
    assert "outdoor" in result["fields"]["description"][1]


def test_details_preserve_all_source_fields(seeded_catalog):
    source = json.loads(DEFAULT_SOURCE.read_text(), parse_float=Decimal)
    for record in source["products"]:
        result = catalog.get_product.invoke({"product_id": record["id"]})
        for key, value in record.items():
            assert result[key] == (format(value, ".2f") if isinstance(value, Decimal) else value)
        json.dumps(result)  # Tool output must serialize without a custom encoder.


def test_filters_and_sorting(seeded_catalog):
    assert len(catalog.search_products.invoke({})) == 5
    rows = catalog.search_products.invoke({"brand": "pRoLiNk", "on_sale": True, "in_stock_only": True, "sort": "price_desc", "limit": 100})
    assert rows
    prices = [Decimal(p["selling_price"]) for p in rows]
    assert prices == sorted(prices, reverse=True)
    for row in rows:
        details = catalog.get_product.invoke({"product_id": row["id"]})
        assert details["brand"].lower() == "prolink"
        assert details["sale_price"] is not None and details["stock"] > 0
    offers = catalog.search_products.invoke({"category_id": "CAT-02", "on_sale": True, "sort": "discount", "in_stock_only": True})
    assert [row["id"] for row in offers] == ["PRD-05", "PRD-04"]
    regular = catalog.search_products.invoke({"on_sale": False, "limit": 100})
    assert regular and all(p["discount_percent"] == 0 for p in regular)
    assert catalog.search_products.invoke({"query": "no-such-product"}) == []
    assert catalog.search_products.invoke({"query": "%"}) == []
    assert catalog.search_products.invoke({"query": "' OR 1=1 --"}) == []
    assert catalog.search_products.invoke({"query": "TAPO C200"})[0]["id"] == "PRD-06"


def test_missing_products_and_comparison_order(seeded_catalog):
    for function in (catalog.get_product, catalog.check_stock):
        assert function.invoke({"product_id": "missing"}) == {"error": "product_not_found", "product_ids": ["missing"]}
    assert catalog.compare_products.invoke({"product_ids": ["PRD-06", "missing"]}) == {"error": "product_not_found", "product_ids": ["missing"]}
    result = catalog.compare_products.invoke({"product_ids": ["PRD-07", "PRD-06", "PRD-01"]})
    assert result["fields"]["id"] == ["PRD-07", "PRD-06", "PRD-01"]


@pytest.mark.parametrize("tool,args", [
    (catalog.search_products, {"limit": 0}),
    (catalog.search_products, {"limit": 101}),
    (catalog.search_products, {"max_price": -1}),
    (catalog.search_products, {"max_price": "NaN"}),
    (catalog.search_products, {"sort": "invalid"}),
    (catalog.search_products, {"query": "  "}),
    (catalog.search_products, {"unknown": True}),
    (catalog.get_product, {"product_id": ""}),
    (catalog.compare_products, {"product_ids": ["PRD-01"]}),
    (catalog.compare_products, {"product_ids": ["PRD-01"] * 2}),
    (catalog.compare_products, {"product_ids": ["PRD-01", "PRD-02", "PRD-03", "PRD-04"]}),
])
def test_invalid_inputs(tool, args):
    with pytest.raises(ValidationError):
        tool.invoke(args)


def test_live_data_and_read_only_transactions(seeded_catalog):
    with Session(seeded_catalog) as session, session.begin():
        product = session.get(Product, "PRD-06")
        product.sale_price = Decimal("4321.09")
        product.stock = 1
        session.add(Category(id="EMPTY", name="Empty category"))
    assert catalog.search_products.invoke({"query": "TAPO C200", "max_price": "4321.09"})[0]["selling_price"] == "4321.09"
    assert catalog.search_products.invoke({"query": "TAPO C200", "max_price": "4321.08"}) == []
    assert catalog.check_stock.invoke({"product_id": "PRD-06"})["label"] == "Only 1 left"
    assert catalog.list_categories.invoke({})[-1]["product_count"] == 0
    with catalog.catalog_session() as session:
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        with pytest.raises(DBAPIError):
            session.execute(text("CREATE TABLE forbidden_write (id integer)"))

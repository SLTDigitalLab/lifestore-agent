"""Read-only SQL catalog tools. Monetary outputs are exact decimal strings in LKR."""

from contextlib import contextmanager
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Category, Product
from app.db.session import get_engine
from app.core.audit import audited

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sort = Literal["price_asc", "price_desc", "discount"]


class CatalogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListCategoriesInput(CatalogInput):
    pass


class SearchProductsInput(CatalogInput):
    query: NonEmpty | None = Field(default=None, description="Keywords in name, brand or description; all words must match.")
    category_id: NonEmpty | None = None
    brand: NonEmpty | None = Field(default=None, description="Exact brand name, case-insensitive.")
    max_price: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    on_sale: bool | None = Field(default=None, description="True: sale price present; False: no sale price; omitted: both.")
    in_stock_only: bool = False
    sort: Sort | None = None
    limit: int = Field(default=5, ge=1, le=100, strict=True)


class ProductInput(CatalogInput):
    product_id: NonEmpty


class CompareProductsInput(CatalogInput):
    product_ids: list[NonEmpty] = Field(min_length=2, max_length=3)

    @field_validator("product_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Choose 2 or 3 distinct product IDs")
        return value


@lru_cache(maxsize=1)
def get_catalog_engine():
    # Lazy initialization: importing tool definitions does not open a database.
    return get_engine()


@contextmanager
def catalog_session():
    # PostgreSQL enforces read-only transactions, beyond issuing SELECTs only.
    with get_catalog_engine().connect().execution_options(postgresql_readonly=True) as connection:
        with Session(bind=connection) as session:
            yield session


def summary(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "selling_price": format(product.selling_price, ".2f"),
        "discount_percent": product.discount_percent,
        "stock_label": product.stock_label,
        "url": product.url,
    }


def details(product: Product) -> dict:
    fields = {column.key: getattr(product, column.key) for column in Product.__table__.columns}
    fields = {key: format(value, ".2f") if isinstance(value, Decimal) else value for key, value in fields.items()}
    return {**fields, **summary(product)}


def missing_products(ids: list[str]) -> dict:
    return {"error": "product_not_found", "product_ids": ids}


@tool(args_schema=ListCategoriesInput)
@audited()
def list_categories() -> list[dict]:
    """List every LifeStore category with its current SQL product count."""
    statement = (
        select(Category.id, Category.name, func.count(Product.id).label("product_count"))
        .outerjoin(Product, Product.category_id == Category.id)
        .group_by(Category.id, Category.name)
        .order_by(Category.id)
    )
    with catalog_session() as session:
        return [dict(row) for row in session.execute(statement).mappings()]


@tool(args_schema=SearchProductsInput)
@audited()
def search_products(
    query: str | None = None,
    category_id: str | None = None,
    brand: str | None = None,
    max_price: Decimal | None = None,
    on_sale: bool | None = None,
    in_stock_only: bool = False,
    sort: Sort | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search LifeStore using SQL filters; prices are exact LKR decimal strings."""
    selling_price = func.coalesce(Product.sale_price, Product.price)
    statement = select(Product)
    if query:
        for word in query.split():
            statement = statement.where(or_(
                Product.name.icontains(word, autoescape=True),
                Product.brand.icontains(word, autoescape=True),
                Product.description.icontains(word, autoescape=True),
            ))
    if category_id is not None:
        statement = statement.where(Product.category_id == category_id)
    if brand is not None:
        statement = statement.where(func.lower(Product.brand) == brand.lower())
    if max_price is not None:
        statement = statement.where(selling_price <= max_price)
    if on_sale is not None:
        statement = statement.where(Product.sale_price.is_not(None) if on_sale else Product.sale_price.is_(None))
    if in_stock_only:
        statement = statement.where(Product.stock > 0)
    if sort == "price_asc":
        statement = statement.order_by(selling_price.asc())
    elif sort == "price_desc":
        statement = statement.order_by(selling_price.desc())
    elif sort == "discount":
        discount = case(
            (Product.sale_price.is_not(None) & (Product.sale_price != 0),
             (Product.price - Product.sale_price) / func.nullif(Product.price, 0) * 100),
            else_=0,
        )
        statement = statement.order_by(discount.desc())
    statement = statement.order_by(Product.id).limit(limit)
    with catalog_session() as session:
        return [summary(product) for product in session.scalars(statement)]


@tool(args_schema=ProductInput)
@audited()
def get_product(product_id: str) -> dict:
    """Get all stored product fields plus selling price, discount and stock label."""
    with catalog_session() as session:
        product = session.get(Product, product_id)
        return details(product) if product is not None else missing_products([product_id])


@tool(args_schema=ProductInput)
@audited()
def check_stock(product_id: str) -> dict:
    """Get a product's current stock quantity and availability label."""
    with catalog_session() as session:
        product = session.get(Product, product_id)
        if product is None:
            return missing_products([product_id])
        return {"product_id": product.id, "name": product.name, "stock": product.stock, "label": product.stock_label}


@tool(args_schema=CompareProductsInput)
@audited()
def compare_products(product_ids: list[str]) -> dict:
    """Compare 2-3 distinct products, with field values aligned to input ID order."""
    with catalog_session() as session:
        products = {product.id: product for product in session.scalars(select(Product).where(Product.id.in_(product_ids)))}
        missing = [product_id for product_id in product_ids if product_id not in products]
        if missing:
            return missing_products(missing)
        rows = [details(products[product_id]) for product_id in product_ids]
        return {"product_ids": product_ids, "fields": {field: [row[field] for row in rows] for field in rows[0]}}


catalog_tools = [list_categories, search_products, get_product, check_stock, compare_products]

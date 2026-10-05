"""Business fields from lifestore_sample_db.json, mapped to relational tables."""

from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def stock_label(stock: int) -> str:
    if stock == 0:
        return "Out of stock"
    if stock < 5:
        return f"Only {stock} left"
    return "In stock"


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"))
    brand: Mapped[str]
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    stock: Mapped[int]
    warranty_months: Mapped[int]
    description: Mapped[str]
    url: Mapped[str]

    @property
    def selling_price(self) -> Decimal:
        return self.sale_price if self.sale_price is not None else self.price

    @property
    def discount_percent(self) -> int:
        return round((self.price - self.sale_price) / self.price * 100) if self.sale_price else 0

    @property
    def stock_label(self) -> str:
        return stock_label(self.stock)


class Cart(Base):
    __tablename__ = "carts"

    id: Mapped[str] = mapped_column(primary_key=True)
    session_id: Mapped[str]
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str]
    items: Mapped[list["CartItem"]] = relationship(cascade="all, delete-orphan")


class CartItem(Base):
    __tablename__ = "cart_items"

    # The JSON nesting supplies cart_id; no artificial item ID is needed.
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id"), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), primary_key=True)
    quantity: Mapped[int]
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(primary_key=True)
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id"))
    customer_name: Mapped[str]
    phone: Mapped[str]
    email: Mapped[str]
    address: Mapped[str]
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str]
    payhere_payment_id: Mapped[str | None]
    payhere_status_code: Mapped[int]


class PaymentReview(Base):
    """Durable review flags; existing order/sample fields remain unchanged."""
    __tablename__ = "payment_reviews"

    id: Mapped[str] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    payment_id: Mapped[str]
    reason: Mapped[str]

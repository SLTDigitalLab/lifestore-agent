"""Order creation, status lookup, and links to signed PayHere checkout."""

import hashlib
from decimal import Decimal
from functools import lru_cache
from uuid import NAMESPACE_URL, uuid5

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Cart, Order, Product
from app.db.session import get_engine
from app.core.audit import audited
from app.tools.catalog import NonEmpty


class PlaceOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: NonEmpty
    customer_name: NonEmpty
    phone: NonEmpty
    email: NonEmpty
    address: NonEmpty


class OrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: NonEmpty


@lru_cache(maxsize=1)
def get_order_engine():
    return get_engine()


def order_result(order: Order) -> dict:
    return {"order_id": order.id, "cart_id": order.cart_id, "total": format(order.total, ".2f"),
            "status": order.status, "payhere_payment_id": order.payhere_payment_id,
            "payhere_status_code": order.payhere_status_code}


def snapshot_signature(snapshot: dict) -> tuple:
    return (snapshot["id"], Decimal(snapshot["total"]), sorted(
        (i["product_id"], i["quantity"], Decimal(i["unit_price"])) for i in snapshot["items"]))


@audited("place_order")
def create_order(data: PlaceOrderInput, expected_cart: dict | None = None) -> dict:
    """Trusted checkout service; expected_cart binds approval to exact line prices.

    A converted cart's existing order is returned on graph replay, so an order
    commit followed by a checkpoint failure cannot create a duplicate order.
    """
    with Session(get_order_engine()) as session, session.begin():
        key = int.from_bytes(hashlib.sha256(("lifestore-cart:" + data.session_id).encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        statement = select(Cart).where(Cart.session_id == data.session_id)
        statement = statement.where(Cart.id == expected_cart["id"]) if expected_cart else statement.where(Cart.status == "OPEN")
        carts = session.scalars(statement.with_for_update()).all()
        if not carts:
            return {"error": "empty_cart"}
        if len(carts) != 1:
            return {"error": "multiple_open_carts"}
        cart = carts[0]
        existing = session.scalar(select(Order).where(Order.cart_id == cart.id))
        if existing is not None:
            return order_result(existing)
        if cart.status != "OPEN":
            return {"error": "cart_not_open"}
        if not cart.items:
            return {"error": "empty_cart"}
        products = {p.id: p for p in session.scalars(select(Product).where(
            Product.id.in_([i.product_id for i in cart.items])).order_by(Product.id).with_for_update(read=True))}
        lines = []
        total = Decimal("0.00")
        for item in cart.items:
            product = products[item.product_id]
            if item.quantity > product.stock:
                return {"error": "insufficient_stock", "product_id": product.id, "available_stock": product.stock}
            lines.append({"product_id": product.id, "quantity": item.quantity, "unit_price": format(product.selling_price, ".2f")})
            total += item.quantity * product.selling_price
        current = {"id": cart.id, "items": lines, "total": format(total, ".2f")}
        if expected_cart is not None and snapshot_signature(current) != snapshot_signature(expected_cart):
            return {"error": "cart_changed"}
        for item in cart.items:
            item.unit_price = products[item.product_id].selling_price
        cart.total = total
        cart.status = "CONVERTED"
        order = Order(id="ORD-" + uuid5(NAMESPACE_URL, "lifestore:" + cart.id).hex,
                      cart_id=cart.id, total=total, status="PENDING", payhere_payment_id=None,
                      payhere_status_code=0, **data.model_dump(exclude={"session_id"}))
        session.add(order)
        session.flush()
        return order_result(order)


@tool(args_schema=PlaceOrderInput)
def place_order(session_id: str, customer_name: str, phone: str, email: str, address: str) -> dict:
    """Create a pending order from the live cart. Trusted callers must obtain confirmation first."""
    return create_order(PlaceOrderInput(session_id=session_id, customer_name=customer_name, phone=phone, email=email, address=address))


@tool(args_schema=OrderInput)
@audited()
def get_order_status(order_id: str) -> dict:
    """Read order and payment status from PostgreSQL."""
    with Session(get_order_engine()) as session:
        order = session.get(Order, order_id)
        return order_result(order) if order else {"error": "order_not_found", "order_id": order_id}


@tool(args_schema=OrderInput)
@audited()
def get_payment_link(order_id: str) -> dict:
    """Return an expiring link to a server-generated signed PayHere sandbox form."""
    status = get_order_status.invoke({"order_id": order_id})
    if "error" in status:
        return status
    if status["status"] == "PAID":
        return {"error": "already_paid", "order_id": order_id}
    from app.api.payhere import payment_link
    return payment_link(order_id)


order_tools = [place_order, get_payment_link, get_order_status]

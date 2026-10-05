"""Session-scoped carts with transactional updates and live catalog values."""

import hashlib
from decimal import Decimal
from functools import lru_cache
from uuid import uuid4

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Cart, CartItem, Product
from app.db.session import get_engine
from app.core.audit import audited
from app.tools.catalog import NonEmpty


class CartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: NonEmpty


class CartProductInput(CartInput):
    product_id: NonEmpty


class AddToCartInput(CartProductInput):
    quantity: int = Field(gt=0, strict=True)


class UpdateCartItemInput(CartProductInput):
    quantity: int = Field(ge=0, strict=True)


@lru_cache(maxsize=1)
def get_cart_engine():
    return get_engine()


def _empty(session_id: str) -> dict:
    return {"id": None, "session_id": session_id, "items": [], "total": "0.00", "status": "OPEN"}


def _operate(session_id: str, action: str, product_id: str | None = None, quantity: int = 0) -> dict:
    with Session(get_cart_engine()) as session, session.begin():
        # A transaction-level session lock also covers creation of the first cart.
        lock_id = int.from_bytes(hashlib.sha256(("lifestore-cart:" + session_id).encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
        carts = session.scalars(select(Cart).where(Cart.session_id == session_id, Cart.status == "OPEN").with_for_update()).all()
        if len(carts) > 1:
            return {"error": "multiple_open_carts", "session_id": session_id}
        cart = carts[0] if carts else None
        items = {item.product_id: item for item in cart.items} if cart else {}
        ids = set(items)
        if product_id:
            ids.add(product_id)
        products = {p.id: p for p in session.scalars(
            select(Product).where(Product.id.in_(ids)).order_by(Product.id).with_for_update(read=True)
        )} if ids else {}

        # Validate before changing anything: refused requests leave even prices
        # and totals untouched. Add checks the combined quantity, not just delta.
        if action in ("add", "update") and quantity > 0:
            product = products.get(product_id)
            if product is None:
                return {"error": "product_not_found", "product_id": product_id}
            if action == "update" and product_id not in items:
                return {"error": "cart_item_not_found", "product_id": product_id}
            requested = quantity + (items[product_id].quantity if action == "add" and product_id in items else 0)
            if requested > product.stock:
                return {"error": "insufficient_stock", "product_id": product_id,
                        "requested_quantity": requested, "available_stock": product.stock}
            if cart is None:
                cart = Cart(id="CART-" + uuid4().hex, session_id=session_id,
                            total=Decimal("0.00"), status="OPEN", items=[])
                session.add(cart)
            if product_id in items:
                items[product_id].quantity = requested
            else:
                item = CartItem(product_id=product_id, quantity=requested, unit_price=product.selling_price)
                cart.items.append(item)
                items[product_id] = item
        elif action in ("remove", "update") and product_id in items:
            cart.items.remove(items.pop(product_id))

        if cart is None:
            return _empty(session_id)

        result = []
        total = Decimal("0.00")
        for item in sorted(items.values(), key=lambda i: i.product_id):
            product = products[item.product_id]
            item.unit_price = product.selling_price
            subtotal = item.unit_price * item.quantity
            total += subtotal
            result.append({"product_id": item.product_id, "name": product.name,
                           "quantity": item.quantity, "unit_price": format(item.unit_price, ".2f"),
                           "subtotal": format(subtotal, ".2f"), "stock": product.stock,
                           "stock_label": product.stock_label,
                           "available": item.quantity <= product.stock})
        cart.total = total
        return {"id": cart.id, "session_id": session_id, "items": result,
                "total": format(total, ".2f"), "status": cart.status}


@tool(args_schema=CartInput)
@audited()
def view_cart(session_id: str) -> dict:
    """View the open cart, refreshing saved prices and total from live products."""
    return _operate(session_id, "view")


@tool(args_schema=AddToCartInput)
@audited()
def add_to_cart(session_id: str, product_id: str, quantity: int) -> dict:
    """Add quantity to the open cart; return an error if combined quantity exceeds live stock."""
    return _operate(session_id, "add", product_id, quantity)


@tool(args_schema=UpdateCartItemInput)
@audited()
def update_cart_item(session_id: str, product_id: str, quantity: int) -> dict:
    """Set an existing item's quantity; zero removes it. Recheck live price and stock."""
    return _operate(session_id, "update", product_id, quantity)


@tool(args_schema=CartProductInput)
@audited()
def remove_from_cart(session_id: str, product_id: str) -> dict:
    """Remove an item if present and return the refreshed open cart."""
    return _operate(session_id, "remove", product_id)


cart_tools = [view_cart, add_to_cart, update_cart_item, remove_from_cart]

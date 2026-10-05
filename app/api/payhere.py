"""Signed PayHere checkout and independently verified server notifications."""

import hashlib
import hmac
import html
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Cart, Order, Product, PaymentReview
from app.tools.orders import get_order_engine

router = APIRouter()
logger = logging.getLogger(__name__)


def md5u(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest().upper()


def make_hash(merchant_id, order_id, amount, secret, currency="LKR"):
    return md5u(f"{merchant_id}{order_id}{amount:.2f}{currency}{md5u(secret)}")


def verify(p, secret):
    try:
        expected = md5u(p["merchant_id"] + p["order_id"] + p["payhere_amount"]
                       + p["payhere_currency"] + p["status_code"] + md5u(secret))
        return hmac.compare_digest(expected, p["md5sig"])
    except (KeyError, TypeError):
        return False


def settings():
    merchant = os.getenv("PAYHERE_MERCHANT_ID", "")
    secret = os.getenv("PAYHERE_MERCHANT_SECRET", "")
    base = os.getenv("PAYHERE_PUBLIC_BASE_URL", "").rstrip("/")
    if not merchant or not secret or not base.startswith("https://"):
        raise ValueError("Configure PayHere merchant credentials and HTTPS PAYHERE_PUBLIC_BASE_URL")
    if os.getenv("PAYHERE_MODE", "sandbox") != "sandbox":
        raise ValueError("Only PayHere sandbox is enabled in this phase")
    return merchant, secret, base


def _token(order_id, expiry, secret):
    return hmac.new(secret.encode(), f"checkout:{order_id}:{expiry}".encode(), hashlib.sha256).hexdigest()


def payment_link(order_id):
    try:
        _, secret, base = settings()
    except ValueError as error:
        return {"error": "payment_not_configured", "message": str(error)}
    expiry = int(time.time()) + 3600
    query = urlencode({"order_id": order_id, "expires": expiry, "token": _token(order_id, expiry, secret)})
    return {"order_id": order_id, "url": f"{base}/payments/payhere?{query}", "expires": expiry}


@router.get("/payments/payhere", response_class=HTMLResponse)
def checkout_form(order_id: str, expires: int, token: str):
    try:
        merchant, secret, base = settings()
    except ValueError as error:
        raise HTTPException(503, str(error)) from error
    if expires < time.time() or not hmac.compare_digest(token, _token(order_id, expires, secret)):
        raise HTTPException(403, "Invalid or expired payment link")
    with Session(get_order_engine()) as session:
        order = session.get(Order, order_id)
        if not order:
            raise HTTPException(404, "Order not found")
        if order.status == "PAID":
            raise HTTPException(409, "Order already paid")
        cart = session.get(Cart, order.cart_id)
        # Existing schema stores the whole delivery address; the final comma-
        # separated component must be the customer's city, never an invented value.
        if "," not in order.address or not order.address.rsplit(",", 1)[1].strip():
            raise HTTPException(422, "Delivery address must end with a comma and city")
        first, _, last = order.customer_name.partition(" ")
        fields = dict(merchant_id=merchant, return_url=base + "/payments/payhere/return",
                      cancel_url=base + "/payments/payhere/cancel", notify_url=base + "/webhooks/payhere",
                      first_name=first, last_name=last, email=order.email, phone=order.phone,
                      address=order.address, city=order.address.rsplit(",", 1)[1].strip(), country="Sri Lanka",
                      order_id=order.id, items="LifeStore order " + order.id, currency="LKR",
                      amount=format(order.total, ".2f"), custom_1=cart.session_id,
                      hash=make_hash(merchant, order.id, order.total, secret))
    inputs = "".join(f'<input type="hidden" name="{html.escape(k, quote=True)}" value="{html.escape(v, quote=True)}">' for k, v in fields.items())
    body = ('<!doctype html><html><head><title>LifeStore payment</title></head><body>'
            '<p>Redirecting to PayHere sandbox...</p><form id="payment" method="post" '
            'action="https://sandbox.payhere.lk/pay/checkout">' + inputs +
            '<noscript><button type="submit">Continue to PayHere</button></noscript></form>'
            '<script>document.getElementById("payment").submit();</script></body></html>')
    # PayHere validates the originating domain; send only the origin, never the
    # signed checkout URL or its bearer token.
    return HTMLResponse(body, headers={"Cache-Control": "no-store", "Referrer-Policy": "strict-origin"})


@router.get("/payments/payhere/return", response_class=HTMLResponse)
@router.get("/payments/payhere/cancel", response_class=HTMLResponse)
def payment_return():
    return "<p>Return to your LifeStore conversation to check the verified order status.</p>"


@router.post("/webhooks/payhere")
async def notify(request: Request):
    secret = os.getenv("PAYHERE_MERCHANT_SECRET", "")
    merchant = os.getenv("PAYHERE_MERCHANT_ID", "")
    if not secret or not merchant:
        raise HTTPException(503, "PayHere credentials not configured")
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/x-www-form-urlencoded":
        raise HTTPException(400, "Expected form-encoded notification")
    body = await request.body()
    if len(body) > 16384:
        raise HTTPException(400, "Notification too large")
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    except (ValueError, UnicodeError) as error:
        raise HTTPException(400, "Invalid form") from error
    p = dict(pairs)
    if len(p) != len(pairs) or not verify(p, secret):
        logger.warning("Rejected PayHere notification: invalid signature or duplicate fields")
        raise HTTPException(400, "Invalid signature")
    # No database access or mutation precedes signature verification.
    try:
        amount = Decimal(p["payhere_amount"])
        code = int(p["status_code"])
    except (KeyError, ValueError, InvalidOperation) as error:
        raise HTTPException(400, "Invalid notification values") from error
    if not amount.is_finite() or amount < 0 or p["merchant_id"] != merchant or p["payhere_currency"] != "LKR" or code not in (2, 0, -1, -2, -3):
        raise HTTPException(400, "Invalid merchant, currency, amount or status")
    payment_id = p.get("payment_id", "")
    if not payment_id:
        raise HTTPException(400, "Missing payment_id")
    with Session(get_order_engine()) as session, session.begin():
        order = session.scalar(select(Order).where(Order.id == p["order_id"]).with_for_update())
        if order is None:
            raise HTTPException(404, "Order not found")
        if amount != order.total:
            raise HTTPException(400, "Payment amount does not match order")
        if code == -3:
            review_id = hashlib.sha256(f"chargeback:{order.id}:{payment_id}".encode()).hexdigest()
            if session.get(PaymentReview, review_id) is None:
                session.add(PaymentReview(id=review_id, order_id=order.id, payment_id=payment_id, reason="CHARGEBACK"))
            logger.warning("PayHere chargeback requires review for order %s", order.id)
            return {"status": "review_required"}
        # PAID is terminal here; retries and delayed failures cannot deduct again
        # or regress payment state. Refund handling belongs to manual review.
        if order.status == "PAID":
            return {"status": "ok", "duplicate_or_stale": True}
        if code == 2:
            cart = session.get(Cart, order.cart_id)
            products = {p.id: p for p in session.scalars(select(Product).where(
                Product.id.in_([i.product_id for i in cart.items])).order_by(Product.id).with_for_update())}
            if not cart.items or any(products[i.product_id].stock < i.quantity for i in cart.items):
                logger.error("Paid notification needs stock reconciliation for order %s", order.id)
                raise HTTPException(409, "Stock reconciliation required; order unchanged")
            for item in cart.items:
                products[item.product_id].stock -= item.quantity
            order.status = "PAID"
        else:
            order.status = "PENDING" if code == 0 else "PAYMENT_FAILED"
        order.payhere_payment_id = payment_id
        order.payhere_status_code = code
    return {"status": "ok"}

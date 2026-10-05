"""Shopping tools bind identity server-side; order confirmation stays outside the LLM."""
from functools import lru_cache

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.graph.browsing import SYSTEM_PROMPT, build_browsing_graph
from app.graph.checkout import build_checkout_graph
from app.tools.catalog import catalog_tools
from app.tools import cart


@lru_cache(maxsize=1)
def checkout_graph():
    return build_checkout_graph()


def identity(config):
    return config["configurable"]["thread_id"]


@tool
def shopping_cart(config: RunnableConfig) -> dict:
    """View this customer's cart with current prices."""
    return cart.view_cart.invoke({"session_id": identity(config)}, config=config)


@tool
def set_cart_item(product_id: str, quantity: int, config: RunnableConfig) -> dict:
    """Set an exact product quantity in this customer's cart; zero removes it."""
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        return {"error": "invalid_quantity"}
    args = {"session_id": identity(config), "product_id": product_id, "quantity": quantity}
    current = shopping_cart.invoke({}, config=config)
    if "error" in current:
        return current
    exists = any(i["product_id"] == product_id for i in current["items"])
    if not exists and quantity == 0:
        return current
    operation = cart.update_cart_item if exists else cart.add_to_cart
    return operation.invoke(args, config=config)


@tool
def prepare_checkout(customer_name: str, phone: str, email: str, address: str,
                     config: RunnableConfig) -> dict:
    """Prepare an order review after collecting actual customer details. Never places an order.

    Address must end with comma and city. The customer must click Confirm order next.
    """
    if not all(v.strip() for v in (customer_name, phone, email, address)):
        return {"error": "missing_customer_details"}
    if "@" not in email or "," not in address or not address.rsplit(",", 1)[1].strip():
        return {"error": "provide_valid_email_and_address_ending_in_comma_city"}
    # A tool call inherits the browsing checkpoint namespace. Checkout owns its
    # own durable graph and must use the same root config as the HTTP endpoint.
    config = {"configurable": {"thread_id": identity(config)}}
    graph = checkout_graph()
    if graph.get_state(config).next:
        return {"error": "review_pending_use_confirm_or_cancel_buttons"}
    result = graph.invoke(dict(session_id=identity(config), customer_name=customer_name,
                               phone=phone, email=email, address=address), config)
    if result.get("error"):
        return result["error"]
    return {"review_required": True, "cart": result["cart"],
            "message": "Use the order review buttons to confirm or cancel. Payment is sandbox only."}


@tool
def my_orders(config: RunnableConfig) -> list[dict]:
    """Read this conversation's actual order/payment status; PAID means verified by PayHere."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db.models import Order, Cart
    from app.tools.orders import get_order_engine
    with Session(get_order_engine()) as db:
        orders = db.scalars(select(Order).join(Cart, Order.cart_id == Cart.id)
                            .where(Cart.session_id == identity(config))).all()
        return [{'order_id': o.id, 'status': o.status, 'total': format(o.total, '.2f')} for o in orders]

SHOPPING_PROMPT = SYSTEM_PROMPT.replace(
    "You are the LifeStore product browsing assistant.",
    "You are the LifeStore shopping assistant.")
SHOPPING_PROMPT = SHOPPING_PROMPT[:SHOPPING_PROMPT.index("You only support browsing in this phase.")] + '''
You support catalog browsing, cart updates, and preparing sandbox checkout.
For ambiguous needs, ask what the customer already has and needs before recommending.
A USB Wi-Fi adapter connects one computer; it is not a home router or internet service.
Resolve selected products using catalog tools and conversation history. Never guess IDs.
When the customer wants to buy, ask quantity if missing, then use set_cart_item.
Only change the cart at the customer's request. Exact quantities make retries safe.
Collect name, phone, email and delivery address ending with comma and city; never invent them.
Then call prepare_checkout. The website displays the exact review and confirmation buttons.
You cannot confirm an order or generate a payment URL yourself. Never claim payment succeeded.
Use my_orders for payment/order status questions; never infer payment from a browser return. Never invent payment links. Tell the customer to use the displayed Confirm order button.
Never claim an order is dispatched or out for delivery based only on payment. Sandbox orders have no real delivery. Do not request card details. All payments are on PayHere sandbox. No real charges.
Redirect complaints/refunds to 0112 300 801 / lifestore@slt.lk.
Follow explicit language requests in English, Sinhala or Tamil.
'''


def build_shopping_graph():
    return build_browsing_graph(tools=[*catalog_tools, shopping_cart, set_cart_item, prepare_checkout, my_orders],
                                system_prompt=SHOPPING_PROMPT)

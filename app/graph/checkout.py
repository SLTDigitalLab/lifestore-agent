"""Deterministic checkout subgraph with durable customer confirmation."""

from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from app.graph.checkpoints import get_checkpointer
from app.tools.cart import view_cart
from app.tools.orders import PlaceOrderInput, create_order, get_payment_link


class CheckoutState(TypedDict, total=False):
    session_id: str
    customer_name: str
    phone: str
    email: str
    address: str
    cart: dict
    confirmed: bool
    error: dict | None
    order: dict | None
    payment: dict | None


def _customer(state: CheckoutState, config: RunnableConfig) -> PlaceOrderInput:
    data = PlaceOrderInput.model_validate({k: state[k] for k in PlaceOrderInput.model_fields})
    if config.get("configurable", {}).get("thread_id") != data.session_id:
        raise ValueError("thread_id must equal session_id")
    return data


def build_checkout_graph(checkpointer=None):
    def prepare(state, config: RunnableConfig):
        data = _customer(state, config)
        cart = view_cart.invoke({"session_id": data.session_id}, config=config)
        error = cart if "error" in cart else None
        if not error and not cart["items"]:
            error = {"error": "empty_cart"}
        if not error and any(not i["available"] for i in cart["items"]):
            error = {"error": "insufficient_stock"}
        return {"cart": cart, "confirmed": False, "error": error, "order": None, "payment": None}

    def confirm(state, config: RunnableConfig):
        data = _customer(state, config)
        # The snapshot was persisted by prepare; it is not recomputed on resume.
        answer = interrupt({"action": "confirm_order", "cart": state["cart"],
                            "customer": data.model_dump(exclude={"session_id"})})
        while answer is not True and answer is not False:
            answer = interrupt({"action": "confirm_order", "cart": state["cart"],
                                "message": "Resume with boolean true to confirm or false to cancel."})
        return {"confirmed": answer is True}

    def place(state, config: RunnableConfig):
        data = _customer(state, config)
        if state.get("confirmed") is not True:
            raise ValueError("Explicit confirmation required")
        result = create_order(data, expected_cart=state["cart"])
        return {"error": result if "error" in result else None,
                "order": None if "error" in result else result, "confirmed": False}

    def payment(state, config: RunnableConfig):
        return {"payment": get_payment_link.invoke({"order_id": state["order"]["order_id"]}, config=config)}

    builder = StateGraph(CheckoutState)
    builder.add_node("prepare", prepare)
    builder.add_node("confirm", confirm)
    builder.add_node("place_order", place)
    builder.add_node("payment", payment)
    builder.add_edge(START, "prepare")
    builder.add_conditional_edges("prepare", lambda s: END if s["error"] else "confirm")
    builder.add_conditional_edges("confirm", lambda s: "place_order" if s["confirmed"] else END)
    builder.add_conditional_edges("place_order", lambda s: "prepare" if s["error"] and s["error"]["error"] == "cart_changed" else END if s["error"] else "payment")
    builder.add_edge("payment", END)
    return builder.compile(checkpointer=checkpointer if checkpointer is not None else get_checkpointer("checkout"))

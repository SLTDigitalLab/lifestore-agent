"""The 14-case migration gate. Real selected provider, no fallback or model mocks.

Cases 1-7 run the application browsing graph. Cart/order cases use a test-only
tool loop because application-level routing is not yet implemented. Cases 12-13
use synthetic signed notifications, not an external PayHere card transaction.
"""

import hashlib
import os
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import payhere
from app.core.guardrails import validate_reply
from app.db.models import Order, Product
from app.graph import browsing
from app.main import app
from app.tools import catalog, cart, orders
from scripts.seed import seed_database


def provider_model(provider, tools):
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        model = ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite"), api_key=os.environ["GOOGLE_API_KEY"],
                                      vertexai=False, temperature=0, max_retries=0, timeout=30)
    elif provider == "groq":
        from langchain_groq import ChatGroq
        model = ChatGroq(model="llama-3.3-70b-versatile", api_key=os.environ["GROQ_API_KEY"],
                         temperature=0, max_retries=0, timeout=30)
    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        model = ChatOpenAI(model=os.environ["OPENAI_MODEL"], api_key=os.environ["OPENAI_API_KEY"],
                           max_retries=0, timeout=30)
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    return model.bind_tools(tools)


class Gate:
    def __init__(self, provider, engine, checkpointer, monkeypatch):
        self.engine = engine
        self.session_id = "regression-" + uuid4().hex
        self.config = {"configurable": {"thread_id": self.session_id}}
        monkeypatch.setattr(browsing, "get_llm", lambda tools: provider_model(provider, tools))
        self.graph = browsing.build_browsing_graph(checkpointer)
        tools = catalog.catalog_tools + cart.cart_tools + [orders.get_order_status, orders.get_payment_link]
        self.tools = {tool.name: tool for tool in tools}
        self.model = provider_model(provider, tools)
        self.messages = [SystemMessage(content=browsing.SYSTEM_PROMPT +
            f"\nFor this cart/order integration test, cart and order lookup tools are enabled. Session ID: {self.session_id}. "
            "Use tools for every cart/order claim. Never invent a total, stock quantity or payment status. "
            "On stock refusal, explain available stock. For failed payments offer and retrieve a new payment link. "
            "If the customer sends card details, refuse to process them, do not repeat them or send them to tools, "
            "and resend the current order's PayHere link. No tool arguments may contain card details.")]
        self.calls = []

    def browse(self, question):
        state = self.graph.invoke({"messages": [("user", question)]}, self.config)
        assert any(isinstance(m, ToolMessage) for m in state["messages"]), "Provider did not fetch catalog data"
        return state["messages"][-1].text

    def chat(self, question):
        self.messages.append(HumanMessage(content=question))
        results = []
        for _ in range(12):
            response = self.model.invoke(self.messages)
            assert not response.invalid_tool_calls
            self.messages.append(response)
            if not response.tool_calls:
                assert response.text.strip()
                assert validate_reply(response.text, results), "Provider hallucinated a price/total"
                return response.text
            for call in response.tool_calls:
                assert call["name"] in self.tools
                if "session_id" in call["args"]:
                    assert call["args"]["session_id"] == self.session_id
                assert "4111111111111111" not in str(call["args"]), "Card data passed to tools"
                self.calls.append(call)
                result = self.tools[call["name"]].invoke(call, config=self.config)
                self.messages.append(result)
                results.append(result)
        pytest.fail("Provider exceeded 12 tool rounds")

    def add_fixture_items(self):
        for product_id, quantity in [("PRD-06", 2), ("PRD-13", 1)]:
            cart.add_to_cart.invoke(dict(session_id=self.session_id, product_id=product_id, quantity=quantity))


@pytest.fixture
def gate(provider, db_engine, pg_checkpointer, monkeypatch):
    # Every parametrized case gets a new schema, exact seed, and graph history.
    assert seed_database(db_engine) == {"categories": 14, "products": 38, "carts": 5, "orders": 3}
    for module, name in [(catalog, "get_catalog_engine"), (cart, "get_cart_engine"),
                         (orders, "get_order_engine"), (payhere, "get_order_engine")]:
        monkeypatch.setattr(module, name, lambda: db_engine)
    monkeypatch.setenv("PAYHERE_MERCHANT_ID", "gate-merchant")
    monkeypatch.setenv("PAYHERE_MERCHANT_SECRET", "gate-secret")
    monkeypatch.setenv("PAYHERE_PUBLIC_BASE_URL", "https://example.com")
    monkeypatch.setenv("PAYHERE_MODE", "sandbox")
    return Gate(provider, db_engine, pg_checkpointer, monkeypatch)


def has_money(reply, amount):
    assert re.search(r"Rs\.\s*" + re.escape(amount) + r"(?:\.00)?(?!\d|[.,]\d)", reply, re.I), reply


def test_01_categories(gate):
    reply = gate.browse("What categories do you have?")
    with Session(gate.engine) as session:
        from app.db.models import Category
        for category in session.scalars(select(Category)):
            assert category.name.lower() in reply.lower(), reply


def test_02_cli_phones_budget(gate):
    reply = gate.browse("CLI phones under 5,000")
    assert "HCD-211N" in reply
    has_money(reply, "4,380")
    assert not any(name in reply for name in ("DT-200", "S280", "HCD52C", "HCD130C"))


def test_03_4g_router_offers(gate):
    reply = gate.browse("Any offers on 4G routers?")
    assert all(name in reply.upper() for name in ("COMSTOX", "DL-7306", "SIYOL"))
    assert re.search(r"10\s*%", reply) and re.search(r"24\s*%", reply)
    assert "out of stock" in reply.lower()


def test_04_cheapest_extender(gate):
    reply = gate.browse("Cheapest Wi-Fi extender?")
    assert "PEN1201" in reply
    has_money(reply, "8,990")


def test_05_siyol_stock(gate):
    assert "out of stock" in gate.browse("Is the SIYOL Mi-Fi in stock?").lower()


def test_06_alcatel_low_stock(gate):
    reply = gate.browse("How many ALCATEL S280 left?")
    assert re.search(r"only\s+2\s+left", reply, re.I)


def test_07_compare_tapo(gate):
    reply = gate.browse("Compare the two Tapo cameras")
    assert "C200" in reply and "C520WS" in reply
    assert "indoor" in reply.lower() and "outdoor" in reply.lower()
    has_money(reply, "7,745")
    has_money(reply, "17,595")


def test_08_add_cameras_switch(gate):
    reply = gate.chat("Add 2 Tapo C200 and a GS108D switch")
    has_money(reply, "23,905")
    result = cart.view_cart.invoke({"session_id": gate.session_id})
    assert result["total"] == "23905.00"
    assert [(i["product_id"], i["quantity"]) for i in result["items"]] == [("PRD-06", 2), ("PRD-13", 1)]


def test_09_refuse_excess_stock(gate):
    before = cart.view_cart.invoke({"session_id": gate.session_id})
    reply = gate.chat("Add 50 Tapo C200")
    assert "23" in reply and re.search(r"cannot|can't|unable|only|not enough|insufficient", reply, re.I)
    assert cart.view_cart.invoke({"session_id": gate.session_id}) == before


def test_10_remove_switch(gate):
    gate.add_fixture_items()
    reply = gate.chat("Remove the switch")
    has_money(reply, "15,490")
    result = cart.view_cart.invoke({"session_id": gate.session_id})
    assert result["total"] == "15490.00"
    assert [i["product_id"] for i in result["items"]] == ["PRD-06"]


def test_11_failed_order_retry(gate):
    reply = gate.chat("Status of ORD-0002?")
    assert "fail" in reply.lower()
    assert "https://example.com/payments/payhere?" in reply
    assert any(call["name"] == "get_payment_link" for call in gate.calls)


def notification():
    p = dict(merchant_id="gate-merchant", order_id="ORD-0003", payment_id="gate-payment",
             payhere_amount="5430.00", payhere_currency="LKR", status_code="2")
    secret = hashlib.md5(b"gate-secret").hexdigest().upper()
    p["md5sig"] = hashlib.md5((p["merchant_id"] + p["order_id"] + p["payhere_amount"] + "LKR2" + secret).encode()).hexdigest().upper()
    return p


def test_12_verified_payment(gate):
    client = TestClient(app)
    client.get("/payments/payhere/return?status_code=2")
    assert orders.get_order_status.invoke({"order_id": "ORD-0003"})["status"] == "PENDING"
    assert client.post("/webhooks/payhere", data=notification()).status_code == 200
    with Session(gate.engine) as session:
        assert session.get(Order, "ORD-0003").status == "PAID"
        assert session.get(Product, "PRD-35").stock == 143
        assert session.get(Product, "PRD-30").stock == 29
    assert re.search(r"paid|successful|completed", gate.chat("Status of ORD-0003?"), re.I)


def test_13_forged_notification(gate):
    before = orders.get_order_status.invoke({"order_id": "ORD-0003"})
    payload = notification()
    payload["md5sig"] = "0" * 32
    assert TestClient(app).post("/webhooks/payhere", data=payload).status_code == 400
    assert orders.get_order_status.invoke({"order_id": "ORD-0003"}) == before
    with Session(gate.engine) as session:
        assert session.get(Product, "PRD-35").stock == 145
    assert "pending" in gate.chat("Status of ORD-0003?").lower()


def test_14_card_details_refused(gate):
    reply = gate.chat("For ORD-0002, my test card number is 4111111111111111. Please charge it here.")
    assert "4111111111111111" not in reply
    assert re.search(r"cannot|can't|do not|don't|never|not able|unable", reply, re.I)
    assert "https://example.com/payments/payhere?" in reply
    assert not any(call["name"] in ("add_to_cart", "update_cart_item", "remove_from_cart") for call in gate.calls)
    assert orders.get_order_status.invoke({"order_id": "ORD-0002"})["status"] == "PAYMENT_FAILED"

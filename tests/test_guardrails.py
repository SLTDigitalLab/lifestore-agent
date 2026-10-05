import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.guardrails import validate_reply
from app.core.audit import AuditLog
from app.graph import browsing
from app.tools import catalog, cart
from scripts.seed import seed_database


@pytest.mark.parametrize("reply,results,expected", [
    ("Rs. 26,625", [{"selling_price": "26625.00"}], True),
    ("Rs. 26,625, in stock", [{"selling_price": "26625.00"}], True),
    ("Rs. 99,999", [{"selling_price": "26625.00"}], False),
    ("Rs.8,990 and Rs. 9,255.00", [{"price": "8990.00"}, {"price": "9255.00"}], True),
    ("Rs. 8,990.01", [{"price": "8990.00"}], False),
    ("Rs. 23", [{"stock": 23}], False),
    ("Rs. 100", [{"description": "Rs. 100"}], False),
    ("Rs. 100", [{"error": "failed", "price": "100"}], False),
    ("Rs. 1,00", [{"price": "100"}], False),
    ("Rs. 10.123", [{"price": "10.12"}], False),
    ("No prices available.", [], True),
    ("Rs. 0", [{"sale_price": "0.00"}], True),
    ("Rs. 7,745", [{"fields": {"selling_price": ["7745.00", "17595.00"]}}], True),
])
def test_validation(reply, results, expected):
    assert validate_reply(reply, results) is expected


@pytest.mark.parametrize("repair", [True, False])
def test_wrong_reply_is_never_emitted(db_engine, pg_checkpointer, monkeypatch, caplog, repair):
    seed_database(db_engine)
    monkeypatch.setattr(catalog, "get_catalog_engine", lambda: db_engine)
    calls = []
    def model(messages):
        calls.append(messages)
        if isinstance(messages[-1], HumanMessage):
            return AIMessage(content="", tool_calls=[{"name": "get_product", "args": {"product_id": "PRD-01"}, "id": "price"}])
        if len(calls) == 2 or not repair:
            return AIMessage(content="The price is Rs. 999,999.")
        assert "Use only monetary numbers" in messages[0].text
        data = json.loads(messages[-1].content)
        return AIMessage(content=f'The price is Rs. {float(data["selling_price"]):,.0f}. Want details?')
    monkeypatch.setattr(browsing, "get_llm", lambda _: RunnableLambda(model))
    graph = browsing.build_browsing_graph(pg_checkpointer)
    config = {"configurable": {"thread_id": "guard-test"}}
    updates = list(graph.stream({"messages": [("user", "Price?")]}, config, stream_mode="updates"))
    assert "999,999" not in str(updates)
    messages = graph.get_state(config).values["messages"]
    assert all("999,999" not in m.text for m in messages)
    assert len(calls) == 3
    assert "Blocked ungrounded price" in caplog.text
    assert ("Rs. 26,625" in messages[-1].text) if repair else (messages[-1].text == browsing.SAFE_REPLY["English"])
    with Session(db_engine) as session:
        records = session.scalars(select(AuditLog).where(AuditLog.session_id == "guard-test")).all()
        assert len(records) == 1
        assert records[0].tool_name == "get_product"
        assert records[0].input == {"product_id": "PRD-01"}
        assert records[0].output["selling_price"] == "26625.00"


def test_old_turn_prices_are_rejected(db_engine, pg_checkpointer, monkeypatch):
    monkeypatch.setattr(browsing, "get_llm", lambda _: RunnableLambda(lambda _: AIMessage(content="Rs. 100")))
    graph = browsing.build_browsing_graph(pg_checkpointer)
    state = graph.invoke({"messages": [HumanMessage(content="Old request"),
        AIMessage(content="", tool_calls=[{"name": "get_product", "args": {}, "id": "old"}]),
        ToolMessage(content='{"price":"100.00"}', tool_call_id="old"),
        HumanMessage(content="Current price?")]}, {"configurable": {"thread_id": "stale"}})
    assert state["messages"][-1].text == browsing.SAFE_REPLY["English"]


def test_direct_cart_calls_are_audited(db_engine, monkeypatch):
    seed_database(db_engine)
    monkeypatch.setattr(cart, "get_cart_engine", lambda: db_engine)
    result = cart.add_to_cart.invoke({"session_id": "audit-cart", "product_id": "PRD-06", "quantity": 50})
    with Session(db_engine) as session:
        record = session.scalar(select(AuditLog).where(AuditLog.session_id == "audit-cart"))
        assert record.tool_name == "add_to_cart"
        assert record.output == result and result["error"] == "insufficient_stock"
        assert record.timestamp.tzinfo is not None

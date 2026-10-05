"""Real graph + SQL tools; only model decisions are scripted for offline tests."""

import json
from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from sqlalchemy.orm import Session

from app.db.models import Product
from app.graph import browsing
from app.tools import catalog
from scripts.seed import seed_database


def call(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


def money(value):
    number = Decimal(value)
    return f"Rs. {number:,.0f}" if number == number.to_integral() else f"Rs. {number:,.2f}"


def scripted_model(messages):
    """Respond from real tool payloads, never embed sample prices in the fake."""
    last = messages[-1]
    if isinstance(last, HumanMessage):
        if "second" in last.text.lower():
            previous = next(m for m in reversed(messages) if isinstance(m, ToolMessage) and m.name == "search_products")
            return call("get_product", {"product_id": json.loads(previous.content)[1]["id"]})
        if "extender" in last.text.lower():
            return call("search_products", {"query": "extender", "sort": "price_asc"})
        return call("search_products", {"query": "TeDi vacuum"})
    data = json.loads(last.content)
    if last.name == "search_products" and data[0]["name"] == "TeDi Vacuum Robot":
        return call("get_product", {"product_id": data[0]["id"]})
    rows = data[:2] if isinstance(data, list) else [data]
    content = "; ".join(f'{row["name"]}: {money(row["selling_price"])}, {row["stock_label"]}' for row in rows)
    return AIMessage(content=content + ". Want to compare products?")


@pytest.fixture
def graph_env(db_engine, monkeypatch, pg_checkpointer):
    monkeypatch.setattr(browsing, "get_checkpointer", lambda _: pg_checkpointer)
    seed_database(db_engine)
    monkeypatch.setattr(catalog, "get_catalog_engine", lambda: db_engine)

    def fake_llm(tools):
        assert tools == catalog.catalog_tools
        return RunnableLambda(scripted_model)

    monkeypatch.setattr(browsing, "get_llm", fake_llm)
    return db_engine


def test_sample_1_extenders(graph_env):
    graph = browsing.build_browsing_graph()
    config = {"configurable": {"thread_id": "extenders"}}
    state = graph.invoke({"messages": [("user", "Cheapest Wi-Fi extender?")]}, config)
    answer = state["messages"][-1].text
    assert "Prolink PEN1201" in answer and "Rs. 8,990" in answer
    assert "Prolink DH5201" in answer and "Rs. 9,255" in answer
    assert [type(m) for m in state["messages"]] == [HumanMessage, AIMessage, ToolMessage, AIMessage]
    assert state["language"] == "English"
    assert graph.get_state(config).next == ()


def test_sample_2_out_of_stock(graph_env):
    graph = browsing.build_browsing_graph()
    state = graph.invoke({"messages": [("user", "Is the TeDi vacuum robot available?")]}, {"configurable": {"thread_id": "vacuum"}})
    answer = state["messages"][-1].text
    assert "TeDi Vacuum Robot" in answer
    assert "out of stock" in answer.lower() and "Rs. 159,900" in answer
    assert [m.name for m in state["messages"] if isinstance(m, ToolMessage)] == ["search_products", "get_product"]


def test_memory_resolves_second_product_and_isolates_sessions(graph_env):
    graph = browsing.build_browsing_graph()
    config = {"configurable": {"thread_id": "returning-customer"}}
    graph.invoke({"messages": [("user", "Cheapest Wi-Fi extender?")]}, config)
    # Changed SQL values must be fetched on the follow-up, not copied from memory.
    with Session(graph_env) as session, session.begin():
        session.get(Product, "PRD-20").sale_price = Decimal("9000.25")
    state = graph.invoke({"messages": [("user", "Details on the second one?")]}, config)
    assert "Prolink DH5201" in state["messages"][-1].text
    assert "Rs. 9,000.25" in state["messages"][-1].text
    assert sum(isinstance(m, HumanMessage) for m in state["messages"]) == 2
    other = graph.invoke({"messages": [("user", "Is the TeDi vacuum robot available?")]}, {"configurable": {"thread_id": "other"}})
    assert sum(isinstance(m, HumanMessage) for m in other["messages"]) == 1
    assert "Prolink" not in other["messages"][-1].text


@pytest.mark.parametrize("question,language", [("Hello", "English"), ("ආයුබෝවන්", "Sinhala"), ("வணக்கம்", "Tamil")])
def test_prompt_language_and_no_tool_exit(monkeypatch, question, language, pg_checkpointer):
    monkeypatch.setattr(browsing, "get_checkpointer", lambda _: pg_checkpointer)
    prompts = []

    def model(messages):
        prompts.append(messages)
        return AIMessage(content="Please ask about LifeStore products.")

    monkeypatch.setattr(browsing, "get_llm", lambda tools: RunnableLambda(model))
    graph = browsing.build_browsing_graph()
    config = {"configurable": {"thread_id": "language"}}
    state = graph.invoke({"messages": [("user", question)]}, config)
    assert state["language"] == language
    assert f"Language hint: {language}." in prompts[0][0].text
    for rule in ('never invent a price/stock fact', 'max 5 products per reply', 'format "Rs. 26,625"', 'end every reply with a next step', "reply in the customer's language", '0112 300 801 / lifestore@slt.lk'):
        assert rule in prompts[0][0].text
    state = graph.invoke({"messages": [("user", "?")]}, config)
    assert state["language"] == language
    assert len(prompts) == 2
    assert sum(isinstance(m, SystemMessage) for m in prompts[-1]) == 1
    assert not any(isinstance(m, SystemMessage) for m in state["messages"])
    assert graph.get_state(config).next == ()

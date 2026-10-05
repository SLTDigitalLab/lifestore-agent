"""Opt-in live-model end-to-end browsing checks, against isolated sample data."""

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.graph.browsing import build_browsing_graph
from app.tools import catalog
from scripts.seed import seed_database


@pytest.mark.parametrize("question,expected", [
    ("Cheapest Wi-Fi extender?", ["Prolink PEN1201", "8,990", "Prolink DH5201", "9,255"]),
    ("Is the TeDi vacuum robot available?", ["TeDi", "out of stock", "159,900"]),
])
def test_live_browsing(request, monkeypatch, question, expected):
    if not request.config.getoption("--run-live-llm"):
        pytest.skip("Use --run-live-llm with model keys and TEST_DATABASE_URL")
    engine = request.getfixturevalue("db_engine")
    seed_database(engine)
    monkeypatch.setattr(catalog, "get_catalog_engine", lambda: engine)
    graph = build_browsing_graph(checkpointer=request.getfixturevalue("pg_checkpointer"))
    config = {"configurable": {"thread_id": uuid4().hex}}
    state = graph.invoke({"messages": [("user", question)], "language": "English"}, config)
    assert any(isinstance(m, ToolMessage) for m in state["messages"])
    final = state["messages"][-1]
    assert isinstance(final, AIMessage) and not final.tool_calls
    assert "rs." in final.text.lower()
    for phrase in expected:
        assert phrase.lower() in final.text.lower()
    assert graph.get_state(config).next == ()

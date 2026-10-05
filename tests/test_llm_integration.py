"""Manual live smoke test; skipped unless explicitly enabled."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.llm import get_llm
from app.tools.catalog import catalog_tools, list_categories


def test_live_categories(request):
    if not request.config.getoption("--run-live-llm"):
        pytest.skip("Use --run-live-llm with GOOGLE_API_KEY and GROQ_API_KEY")
    # No SQL execution here: this phase only checks the model's response/tool call.
    response = get_llm(catalog_tools).invoke([HumanMessage(content="What categories do you have?")])
    assert isinstance(response, AIMessage)
    assert not response.invalid_tool_calls
    if response.tool_calls:
        assert all(call["name"] == "list_categories" for call in response.tool_calls)
        for call in response.tool_calls:
            list_categories.args_schema.model_validate(call["args"])
    else:
        assert response.text.strip(), "Expected a nonempty direct answer or a list_categories call"

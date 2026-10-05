"""Exercise real tool binding and fallback orchestration without network calls."""

import pytest
from google.genai.errors import ClientError
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.core import llm
from app.tools.catalog import catalog_tools, list_categories


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(llm, "load_dotenv", lambda: None)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")


def tool_response():
    return ChatResult(generations=[ChatGeneration(message=AIMessage(
        content="",
        tool_calls=[{"name": "list_categories", "args": {}, "id": "call_categories", "type": "tool_call"}],
    ))])


def test_forced_429_returns_fallback_tool_call(configured, monkeypatch):
    calls = []

    def primary(self, messages, **kwargs):
        calls.append(("gemini", messages, kwargs))
        raise ClientError(429, {"error": {"code": 429, "message": "Forced rate limit", "status": "RESOURCE_EXHAUSTED"}})

    def fallback(self, messages, **kwargs):
        calls.append(("groq", messages, kwargs))
        return tool_response()

    monkeypatch.setattr(llm.ChatGoogleGenerativeAI, "_generate", primary)
    monkeypatch.setattr(llm.ChatGroq, "_generate", fallback)
    chain = llm.get_llm(catalog_tools)
    message = HumanMessage(content="What categories do you have?")
    response = chain.invoke([message])

    assert [call[0] for call in calls] == ["gemini", "groq"]
    assert all(call[1] == [message] for call in calls)
    assert all(call[2]["tools"] for call in calls)
    assert response.tool_calls == [{"name": "list_categories", "args": {}, "id": "call_categories", "type": "tool_call"}]
    list_categories.args_schema.model_validate(response.tool_calls[0]["args"])
    assert not response.invalid_tool_calls
    assert chain.runnable.bound.model == "gemini-2.0-flash-lite"
    assert chain.fallbacks[0].bound.model_name == "llama-3.3-70b-versatile"
    assert chain.runnable.bound.max_retries == chain.fallbacks[0].bound.max_retries == 0


def test_primary_success_does_not_call_fallback(configured, monkeypatch):
    monkeypatch.setattr(llm.ChatGoogleGenerativeAI, "_generate", lambda *args, **kwargs: tool_response())

    def unexpected(*args, **kwargs):
        pytest.fail("Fallback must not run after primary success")

    monkeypatch.setattr(llm.ChatGroq, "_generate", unexpected)
    assert llm.get_llm(catalog_tools).invoke("What categories do you have?").tool_calls[0]["name"] == "list_categories"


def test_both_providers_fail_propagates_error(configured, monkeypatch):
    calls = []

    def fail(self, *args, **kwargs):
        calls.append(type(self).__name__)
        raise RuntimeError("Provider unavailable")

    monkeypatch.setattr(llm.ChatGoogleGenerativeAI, "_generate", fail)
    monkeypatch.setattr(llm.ChatGroq, "_generate", fail)
    with pytest.raises(RuntimeError, match="Provider unavailable"):
        llm.get_llm(catalog_tools).invoke("What categories do you have?")
    assert calls == ["ChatGoogleGenerativeAI", "ChatGroq"]


@pytest.mark.parametrize("name", ["GOOGLE_API_KEY", "GROQ_API_KEY"])
def test_missing_key_fails_clearly(configured, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(ValueError, match=name):
        llm.get_llm(catalog_tools)

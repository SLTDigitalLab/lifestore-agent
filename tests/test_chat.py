from types import SimpleNamespace

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.main import app
from app.api import chat


def test_chat_requires_test_token(monkeypatch):
    monkeypatch.setenv("CHAT_TEST_TOKEN", "test-secret")
    assert TestClient(app).post("/api/chat", json={"message": "hello"}).status_code == 401


def test_chat_returns_graph_reply_and_session(monkeypatch):
    monkeypatch.setenv("CHAT_TEST_TOKEN", "test-secret")
    calls = []
    def invoke(state, config):
        calls.append(config)
        return {"messages": [AIMessage(content="Which category would you like?")]}
    monkeypatch.setattr(chat, "chat_graph", lambda: SimpleNamespace(invoke=invoke))
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-secret"}
    response = client.post("/api/chat", headers=headers, json={"message": "categories"})
    assert response.status_code == 200
    data = response.json()
    assert data["reply"] == "Which category would you like?"
    assert calls[0]["configurable"]["thread_id"] == data["session_id"]
    assert client.post("/api/chat", headers=headers, json={"message": "   "}).status_code == 422


def test_chat_hides_provider_error(monkeypatch):
    monkeypatch.setenv("CHAT_TEST_TOKEN", "test-secret")
    def fail():
        raise ValueError("private-provider-data")
    monkeypatch.setattr(chat, "chat_graph", fail)
    response = TestClient(app).post("/api/chat", headers={"Authorization":"Bearer test-secret"}, json={"message":"hello"})
    assert response.status_code == 503
    assert "private-provider-data" not in response.text
    assert not chat.busy.locked()
import pytest

@pytest.fixture(autouse=True)
def empty_checkout(monkeypatch):
    monkeypatch.setenv('CHAT_ALLOW_LEGACY', '1')
    monkeypatch.setattr(chat, 'checkout_graph', lambda: SimpleNamespace(
        get_state=lambda config: SimpleNamespace(next=(), values={})))


def test_confirmation_requires_session(monkeypatch):
    monkeypatch.setenv('CHAT_TEST_TOKEN', 'test-secret')
    response = TestClient(app).post('/api/chat', headers={'Authorization': 'Bearer test-secret'},
                                    json={'action': 'confirm'})
    assert response.status_code == 422


def test_confirmation_without_review_rejected(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('CHAT_TEST_TOKEN', 'test-secret')
    response = TestClient(app).post('/api/chat', headers={'Authorization': 'Bearer test-secret'},
                                    json={'action': 'confirm', 'session_id': str(uuid4())})
    assert response.status_code == 409

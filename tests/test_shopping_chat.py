from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.api import chat
from app.main import app
from app.graph import shopping
from app.graph.checkout import build_checkout_graph
from app.tools import cart, orders
from app.db.models import Order
from scripts.seed import seed_database

@pytest.fixture
def flow(db_engine, monkeypatch):
    seed_database(db_engine)
    monkeypatch.setattr(cart, 'get_cart_engine', lambda: db_engine)
    monkeypatch.setattr(orders, 'get_order_engine', lambda: db_engine)
    monkeypatch.setenv('CHAT_ALLOW_LEGACY', '1')
    monkeypatch.setenv('CHAT_TEST_TOKEN', 'test-secret')
    monkeypatch.setenv('PAYHERE_MERCHANT_ID', 'test-merchant')
    monkeypatch.setenv('PAYHERE_MERCHANT_SECRET', 'test-secret')
    monkeypatch.setenv('PAYHERE_PUBLIC_BASE_URL', 'https://example.com')
    graph = build_checkout_graph(InMemorySaver())
    monkeypatch.setattr(chat, 'checkout_graph', lambda: graph)
    monkeypatch.setattr(shopping, 'checkout_graph', lambda: graph)
    session = str(uuid4())
    config = {'configurable': {'thread_id': session}}
    for _ in range(2):
        shopping.set_cart_item.invoke({'product_id': 'PRD-06', 'quantity': 1}, config=config)
    result = shopping.prepare_checkout.invoke(dict(customer_name='Test Customer', phone='0771234567',
        email='test@example.com', address='12 Test Road, Colombo'), config=config)
    assert result['review_required']
    assert result['cart']['items'][0]['quantity'] == 1
    client = TestClient(app)
    def send(**data):
        return client.post('/api/chat', headers={'Authorization': 'Bearer test-secret'},
                           json={'session_id': session, **data})
    def count():
        with Session(db_engine) as db:
            return db.scalar(select(func.count()).select_from(Order))
    return send, count


def test_text_cannot_confirm_and_button_creates_single_order(flow):
    send, count = flow
    before = count()
    assert send(message='yes confirm and pay').json()['checkout']
    assert count() == before
    response = send(action='confirm').json()
    assert response['payment_url'].startswith('https://example.com/payments/payhere?')
    assert response['checkout'] is None
    assert count() == before + 1
    assert send(action='confirm').json()['payment_url'] == response['payment_url']
    assert count() == before + 1


def test_cancel_never_places_order(flow):
    send, count = flow
    before = count()
    response = send(action='cancel').json()
    assert response['checkout'] is None
    assert response['payment_url'] is None
    assert count() == before

def test_nested_tool_config_uses_root_checkout(flow, monkeypatch):
    from types import SimpleNamespace
    observed = []
    graph = SimpleNamespace(
        get_state=lambda config: (observed.append(config) or SimpleNamespace(next=())),
        invoke=lambda data, config: {'cart': {'items': [], 'total': '0.00'}})
    monkeypatch.setattr(shopping, 'checkout_graph', lambda: graph)
    shopping.prepare_checkout.invoke(dict(customer_name='Test', phone='0771234567',
        email='test@example.com', address='Road, Colombo'), config={'configurable': {
            'thread_id': 'root-session', 'checkpoint_ns': 'tools:nested', 'checkpoint_id': 'unrelated'}})
    assert observed == [{'configurable': {'thread_id': 'root-session'}}]


def test_live_shopping_to_confirmation(request, db_engine, monkeypatch):
    if not request.config.getoption('--run-live-llm'):
        pytest.skip('Opt in to live shopping model test')
    from app.tools import catalog
    from app.graph.browsing import build_browsing_graph
    seed_database(db_engine)
    for module, name in [(cart, 'get_cart_engine'), (orders, 'get_order_engine'), (catalog, 'get_catalog_engine')]:
        monkeypatch.setattr(module, name, lambda: db_engine)
    monkeypatch.setenv('CHAT_ALLOW_LEGACY', '1')
    monkeypatch.setenv('CHAT_TEST_TOKEN', 'test-secret')
    monkeypatch.setenv('PAYHERE_MERCHANT_ID', 'test-merchant')
    monkeypatch.setenv('PAYHERE_MERCHANT_SECRET', 'test-secret')
    monkeypatch.setenv('PAYHERE_PUBLIC_BASE_URL', 'https://example.com')
    checkout = build_checkout_graph(InMemorySaver())
    monkeypatch.setattr(chat, 'checkout_graph', lambda: checkout)
    monkeypatch.setattr(shopping, 'checkout_graph', lambda: checkout)
    graph = build_browsing_graph(checkpointer=InMemorySaver(),
        tools=[*catalog.catalog_tools, shopping.shopping_cart, shopping.set_cart_item, shopping.prepare_checkout],
        system_prompt=shopping.SHOPPING_PROMPT)
    monkeypatch.setattr(chat, 'chat_graph', lambda: graph)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer test-secret'}
    response = client.post('/api/chat', headers=headers, json={'message':
        'I want to buy exactly one CUDY WU1400 AC1300 Wi-Fi High Gain Adaptor. '
        'I understand it is a USB adapter for one computer. Add it to my cart. '
        'My name is Test Customer, phone 0771234567, email test@example.com, '
        'delivery address 12 Test Road, Colombo. Prepare my sandbox checkout for review.'})
    assert response.status_code == 200
    data = response.json()
    if not data.get('checkout'):
        response = client.post('/api/chat', headers=headers, json={'session_id': data['session_id'],
            'message': 'Use the details I provided to prepare the checkout review now.'})
        assert response.status_code == 200
        data = response.json()
    assert data.get('checkout'), data['reply']
    assert data['checkout']['cart']['items'][0]['quantity'] == 1
    assert 'WU1400' in data['checkout']['cart']['items'][0]['name']
    response = client.post('/api/chat', headers=headers, json={'session_id': data['session_id'], 'action': 'confirm'})
    assert response.status_code == 200
    assert response.json()['payment_url'].startswith('https://example.com/payments/payhere?')

def test_full_shopping_graph_without_external_model(db_engine, monkeypatch):
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.runnables import RunnableLambda
    from app.graph import browsing
    steps = [
        ('get_product', {'product_id': 'PRD-17'}),
        ('set_cart_item', {'product_id': 'PRD-17', 'quantity': 1}),
        ('prepare_checkout', dict(customer_name='Test Customer', phone='0771234567',
                                 email='test@example.com', address='12 Test Road, Colombo')),
    ]
    def respond(messages):
        count = sum(isinstance(m, ToolMessage) for m in messages)
        if count < len(steps):
            name, args = steps[count]
            return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': str(count)}])
        return AIMessage(content='Review your order and use Confirm order.')
    monkeypatch.setattr(browsing, 'get_llm', lambda tools: RunnableLambda(respond))
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda name: True))
    test_live_shopping_to_confirmation(request, db_engine, monkeypatch)

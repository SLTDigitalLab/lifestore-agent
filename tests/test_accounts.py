"""Account isolation, refresh/login persistence, and webhook-verified returns."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.api import auth, chat, conversations, payhere
from app.db.models import UserAccount, LoginSession, Conversation, Cart, Order, ChatTurn, utcnow
from app.tools import orders
from scripts.seed import seed_database

PASSWORD = 'a-test-password-with-enough-length'
HEADERS = {'X-LifeStore-Request': '1', 'Origin': 'https://example.com'}


@pytest.fixture
def account_db(db_engine, monkeypatch):
    monkeypatch.delenv('CHAT_ALLOW_LEGACY', raising=False)
    monkeypatch.setenv('PAYHERE_PUBLIC_BASE_URL', 'https://example.com')
    monkeypatch.setenv('PAYHERE_MERCHANT_ID', 'test-merchant')
    monkeypatch.setenv('PAYHERE_MERCHANT_SECRET', 'test-secret')
    monkeypatch.setattr(auth, 'account_engine', lambda: db_engine)
    monkeypatch.setattr(conversations, 'account_engine', lambda: db_engine)
    monkeypatch.setattr(orders, 'get_order_engine', lambda: db_engine)
    monkeypatch.setattr(payhere, 'get_order_engine', lambda: db_engine)
    empty = SimpleNamespace(get_state=lambda config: SimpleNamespace(next=(), values={}))
    monkeypatch.setattr(chat, 'checkout_graph', lambda: empty)
    monkeypatch.setattr(conversations, 'checkout_graph', lambda: empty)
    monkeypatch.setattr(chat, 'chat_graph', lambda: SimpleNamespace(invoke=lambda state, config:
        {'messages': [AIMessage(content='Saved assistant reply')]}))
    seed_database(db_engine)
    password_hash = auth.hash_password(PASSWORD)
    with Session(db_engine) as db, db.begin():
        for username in ('tester', 'other'):
            db.add(UserAccount(id=str(uuid4()), username=username, name=username, password_hash=password_hash))
    return db_engine


def login(username='tester'):
    client = TestClient(app, base_url='https://example.com', headers=HEADERS)
    response = client.post('/api/auth/login', json={'username': username, 'password': PASSWORD})
    assert response.status_code == 200
    return client


def test_cookie_security_password_hash_and_revocation(account_db):
    client = login()
    assert client.get('/api/auth/me').json()['user']['username'] == 'tester'
    token = client.cookies.get(auth.COOKIE)
    response = client.post('/api/auth/login', json={'username': 'tester', 'password': PASSWORD})
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'secure' in cookie and 'samesite=lax' in cookie and 'max-age=' in cookie
    with Session(account_db) as db:
        user = db.scalar(select(UserAccount).where(UserAccount.username == 'tester'))
        assert PASSWORD not in user.password_hash
        assert auth.verify_password(PASSWORD, user.password_hash)
        assert not auth.verify_password('incorrect', user.password_hash)
        assert not db.get(LoginSession, token)
    token = client.cookies.get(auth.COOKIE)
    assert client.post('/api/auth/logout', json={}).status_code == 200
    client.cookies.set(auth.COOKIE, token)
    assert client.get('/api/auth/me').status_code == 401


def test_no_registration_legacy_bypass_or_cross_origin(account_db, monkeypatch):
    client = TestClient(app, base_url='https://example.com')
    assert client.post('/api/auth/register', json={}).status_code == 404
    assert client.post('/api/auth/login', json={'username': 'tester', 'password': PASSWORD}).status_code == 403
    assert client.post('/api/auth/login', headers={**HEADERS, 'Origin': 'https://attacker.example'},
                       json={'username': 'tester', 'password': PASSWORD}).status_code == 403
    monkeypatch.setenv('CHAT_TEST_TOKEN', 'legacy-secret')
    assert client.post('/api/chat', headers={'Authorization': 'Bearer legacy-secret'},
                       json={'message': 'hello'}).status_code == 401


def test_history_survives_refresh_logout_login_and_request_retry(account_db):
    client = login()
    cid = client.post('/api/conversations', json={}).json()['session_id']
    payload = {'message': 'Remember my Wi-Fi question', 'session_id': cid, 'request_id': str(uuid4())}
    response = client.post('/api/chat', json=payload)
    assert response.status_code == 200
    assert client.post('/api/chat', json=payload).status_code == 200
    assert client.post('/api/auth/logout', json={}).status_code == 200
    client = login()
    saved = client.get('/api/conversations/' + cid).json()
    assert saved['messages'] == [{'role': 'user', 'text': payload['message']},
                                 {'role': 'assistant', 'text': 'Saved assistant reply'}]
    assert client.get('/api/conversations').json()['conversations'][0]['id'] == cid
    assert saved['pending'] is False


def test_other_account_cannot_read_or_mutate_conversation(account_db):
    owner = login()
    cid = owner.post('/api/conversations', json={}).json()['session_id']
    other = login('other')
    assert other.get('/api/conversations/' + cid).status_code == 404
    assert other.post('/api/chat', json={'session_id': cid, 'message': 'intrusion'}).status_code == 404
    assert other.post('/api/chat', json={'session_id': cid, 'action': 'confirm'}).status_code == 404
    assert other.get('/api/conversations').json()['conversations'] == []


def test_expired_session_and_login_throttle(account_db):
    client = login()
    with Session(account_db) as db, db.begin():
        for session in db.scalars(select(LoginSession)):
            session.expires_at = utcnow() - timedelta(seconds=1)
    assert client.get('/api/auth/me').status_code == 401
    for _ in range(12):
        response = client.post('/api/auth/login', json={'username': 'tester', 'password': 'wrong'})
    assert response.status_code == 429


def test_payment_return_restores_chat_but_only_callback_marks_paid(account_db):
    import hashlib
    client = login()
    cid = client.post('/api/conversations', json={}).json()['session_id']
    client.post('/api/chat', json={'session_id': cid, 'message': 'My checkout conversation'})
    with Session(account_db) as db, db.begin():
        order = db.get(Order, 'ORD-0003')
        db.get(Cart, order.cart_id).session_id = cid
    returned = client.get('/payments/payhere/return?conversation=' + cid + '&status_code=2', follow_redirects=False)
    assert returned.status_code == 303
    assert 'conversation=' + cid in returned.headers['location']
    assert client.get(returned.headers['location']).status_code == 200
    before = client.get('/api/conversations/' + cid).json()
    assert before['orders'][0]['status'] == 'PENDING'
    assert before['orders'][0]['payment_url']
    fields = dict(merchant_id='test-merchant', order_id='ORD-0003', payment_id='account-test-payment',
                  payhere_amount='5430.00', payhere_currency='LKR', status_code='2')
    raw = ''.join(fields[k] for k in ('merchant_id', 'order_id', 'payhere_amount', 'payhere_currency', 'status_code'))
    raw += hashlib.md5(b'test-secret').hexdigest().upper()
    fields['md5sig'] = hashlib.md5(raw.encode()).hexdigest().upper()
    assert client.post('/webhooks/payhere', data=fields).status_code == 200
    after = client.get('/api/conversations/' + cid).json()
    assert after['messages'][:-1] == before['messages']
    receipt = after['messages'][-1]
    assert receipt['role'] == 'assistant'
    assert 'Your payment of Rs. 5,430 was successful' in receipt['text']
    assert 'ORD-0003' in receipt['text']
    assert 'no real payment or delivery' in receipt['text']
    # Duplicate callbacks and repeated refreshes do not create repeated messages.
    assert client.post('/webhooks/payhere', data=fields).status_code == 200
    assert client.get('/api/conversations/' + cid).json()['messages'] == after['messages']
    assert after['orders'][0]['status'] == 'PAID'
    assert after['orders'][0]['payment_url'] is None
    cancelled = client.get('/payments/payhere/cancel?conversation=' + cid, follow_redirects=False)
    assert cancelled.status_code == 303 and 'payment=cancelled' in cancelled.headers['location']
    assert client.get('/api/conversations/' + cid).json()['orders'][0]['status'] == 'PAID'


def test_legacy_message_import_excludes_tool_calls():
    from langchain_core.messages import HumanMessage, ToolMessage
    from scripts.create_test_account import history_turns
    messages = [HumanMessage(content='Find Wi-Fi'), AIMessage(content='', tool_calls=[{'name': 'search', 'args': {}, 'id': '1'}]),
                ToolMessage(content='private tool output', tool_call_id='1'), AIMessage(content='Here are products')]
    assert history_turns(messages) == [['Find Wi-Fi', 'Here are products']]

@pytest.mark.parametrize('suffix', ['&custom_2', '&', '&&'])
def test_payhere_form_accepts_optional_bare_fields(account_db, suffix):
    import hashlib
    from urllib.parse import urlencode
    fields = dict(merchant_id='test-merchant', order_id='ORD-0003', payment_id='bare-field-test',
                  payhere_amount='5430.00', payhere_currency='LKR', status_code='2')
    raw = ''.join(fields[k] for k in ('merchant_id', 'order_id', 'payhere_amount', 'payhere_currency', 'status_code'))
    fields['md5sig'] = hashlib.md5((raw + hashlib.md5(b'test-secret').hexdigest().upper()).encode()).hexdigest().upper()
    client = TestClient(app)
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    payload = urlencode(fields) + suffix
    assert client.post('/webhooks/payhere', content=payload, headers=headers).status_code == 200
    with Session(account_db) as db:
        assert db.get(Order, 'ORD-0003').status == 'PAID'
    # A bare duplicate of a signed field must still be rejected.
    assert client.post('/webhooks/payhere', content=payload + '&status_code', headers=headers).status_code == 400
    fields['md5sig'] = '0' * 32
    assert client.post('/webhooks/payhere', content=urlencode(fields) + suffix, headers=headers).status_code == 400


def test_existing_paid_order_gets_one_saved_assistant_receipt(account_db):
    client = login()
    cid = client.post('/api/conversations', json={}).json()['session_id']
    with Session(account_db) as db, db.begin():
        order = db.get(Order, 'ORD-0003')
        order.status = 'PAID'
        db.get(Cart, order.cart_id).session_id = cid
    first = client.get('/api/conversations/' + cid).json()
    assert len(first['messages']) == 1
    assert first['messages'][0]['role'] == 'assistant'
    assert 'payment of Rs. 5,430 was successful' in first['messages'][0]['text']
    assert client.get('/api/conversations/' + cid).json()['messages'] == first['messages']
    with Session(account_db) as db:
        turns = db.scalars(select(ChatTurn).where(ChatTurn.conversation_id == cid)).all()
        assert len(turns) == 1 and turns[0].user_text == ''

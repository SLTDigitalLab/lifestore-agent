"""Owned conversations and durable UI history, separate from model/tool messages."""
import os
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.api.auth import current_user, check_write, account_engine
from app.db.models import Conversation, ChatTurn, Cart, Order, utcnow
from app.graph.shopping import checkout_graph
from app.tools.orders import get_payment_link

router = APIRouter(prefix='/api/conversations')


def require_conversation(db, conversation_id, user_id):
    conversation = db.get(Conversation, str(conversation_id))
    if not conversation or conversation.user_id != user_id:
        raise HTTPException(404, 'Conversation not found.')
    return conversation


def order_summaries(db, conversation_id):
    orders = db.scalars(select(Order).join(Cart, Order.cart_id == Cart.id)
                        .where(Cart.session_id == conversation_id)).all()
    result = []
    for order in orders:
        payment = get_payment_link.invoke({'order_id': order.id}) if order.status != 'PAID' else {}
        result.append({'order_id': order.id, 'total': format(order.total, '.2f'),
                       'status': order.status, 'payment_url': payment.get('url')})
    return result


def save_payment_confirmation(db, order):
    """Persist one assistant receipt per verified paid order, in the caller's transaction."""
    if order.status != 'PAID':
        return
    cart = db.get(Cart, order.cart_id)
    conversation = db.get(Conversation, cart.session_id) if cart else None
    if not conversation:
        return
    amount = format(order.total, ',.2f').removesuffix('.00')
    message = f'Thank you! Your payment of Rs. {amount} was successful. We have received your order {order.id}.'
    if os.getenv('PAYHERE_MODE', 'sandbox') == 'sandbox':
        message += '\n\nThis is a sandbox test order, so no real payment or delivery will take place.'
    else:
        message += '\n\nYour order is awaiting fulfilment. Dispatch and delivery have not yet been confirmed.'
    message += '\n\nIs there anything else I can help you with?'
    event_id = str(uuid5(NAMESPACE_URL, 'lifestore:payment-confirmed:' + order.id))
    result = db.execute(insert(ChatTurn).values(id=event_id, conversation_id=conversation.id,
        user_text='', reply=message, created_at=utcnow()).on_conflict_do_nothing(index_elements=['id']))
    if result.rowcount:
        conversation.updated_at = utcnow()


def detail(conversation_id, user_id):
    with Session(account_engine()) as db, db.begin():
        conversation = require_conversation(db, conversation_id, user_id)
        # Also recover receipts for verified payments made before this feature.
        paid = db.scalars(select(Order).join(Cart, Order.cart_id == Cart.id)
                          .where(Cart.session_id == conversation.id, Order.status == 'PAID')).all()
        for order in paid:
            save_payment_confirmation(db, order)
        turns = db.scalars(select(ChatTurn).where(ChatTurn.conversation_id == conversation.id)
                           .order_by(ChatTurn.created_at, ChatTurn.id)).all()
        messages = []
        for turn in turns:
            if turn.user_text:
                messages.append({'role': 'user', 'text': turn.user_text})
            if turn.reply:
                messages.append({'role': 'assistant', 'text': turn.reply})
        orders = order_summaries(db, conversation.id)
        result = {'session_id': conversation.id, 'title': conversation.title,
                  'messages': messages, 'orders': orders, 'pending': any(t.reply is None for t in turns)}
    state = checkout_graph().get_state({'configurable': {'thread_id': str(conversation_id)}})
    result['checkout'] = None
    if state.next:
        result['checkout'] = {'cart': state.values['cart'], 'customer': {
            k: state.values[k] for k in ('customer_name', 'phone', 'email', 'address')}}
    return result


@router.get('')
def list_conversations(response: Response, user=Depends(current_user)):
    response.headers['Cache-Control'] = 'no-store'
    with Session(account_engine()) as db:
        conversations = db.scalars(select(Conversation).where(Conversation.user_id == user['id'])
                                    .order_by(Conversation.updated_at.desc()).limit(100)).all()
        return {'conversations': [{'id': c.id, 'title': c.title, 'updated_at': c.updated_at.isoformat()}
                                  for c in conversations]}


@router.post('', dependencies=[Depends(check_write)])
def create_conversation(user=Depends(current_user)):
    with Session(account_engine()) as db, db.begin():
        conversation = Conversation(id=str(uuid4()), user_id=user['id'])
        db.add(conversation)
        db.flush()
        return {'session_id': conversation.id}


@router.get('/{conversation_id}')
def get_conversation(conversation_id: UUID, response: Response, user=Depends(current_user)):
    response.headers['Cache-Control'] = 'no-store'
    return detail(str(conversation_id), user['id'])


def start_turn(conversation_id, user, request_id, text):
    with Session(account_engine()) as db, db.begin():
        if conversation_id:
            conversation = require_conversation(db, conversation_id, user['id'])
        else:
            conversation = Conversation(id=str(uuid4()), user_id=user['id'])
            db.add(conversation)
            db.flush()
        previous = db.get(ChatTurn, request_id)
        if previous:
            if previous.conversation_id != conversation.id:
                raise HTTPException(409, 'Request already belongs to another conversation.')
            if previous.reply is None:
                raise HTTPException(409, 'This message is still processing. Refresh before trying again.')
            return conversation.id, previous.reply
        db.add(ChatTurn(id=request_id, conversation_id=conversation.id, user_text=text))
        if conversation.title == 'New conversation':
            conversation.title = text[:70]
        conversation.updated_at = utcnow()
        return conversation.id, None


def finish_turn(request_id, reply):
    with Session(account_engine()) as db, db.begin():
        turn = db.get(ChatTurn, request_id)
        turn.reply = reply
        conversation = db.get(Conversation, turn.conversation_id)
        conversation.updated_at = utcnow()

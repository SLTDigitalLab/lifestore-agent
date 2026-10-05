"""Private shopping chat with explicit, durable checkout confirmation."""
import hmac
import logging
import os
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field
from langgraph.types import Command

from app.graph.shopping import build_shopping_graph, checkout_graph

router = APIRouter()
busy = Lock()
logger = logging.getLogger(__name__)


class ChatInput(BaseModel):
    message: str = Field(default="", max_length=2000)
    session_id: UUID | None = None
    action: Literal["confirm", "cancel"] | None = None
    request_id: UUID = Field(default_factory=uuid4)


@lru_cache(maxsize=1)
def chat_graph():
    return build_shopping_graph()


def checkout_payload(graph, config):
    state = graph.get_state(config)
    if state.next:
        return {"cart": state.values["cart"], "customer": {
            k: state.values[k] for k in ("customer_name", "phone", "email", "address")}}
    return None


@router.get("/", response_class=HTMLResponse)
def chat_page():
    return HTMLResponse(Path(__file__).with_name("chat.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get('/assets/chat.js')
def chat_script():
    return FileResponse(Path(__file__).with_name('chat.js'), media_type='text/javascript',
                        headers={'Cache-Control': 'no-cache'})

def process_chat(data, session_id):
    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 16}
    graph = checkout_graph()
    review = checkout_payload(graph, config)
    if data.action:
        if not review:
            # Return the durable outcome on a retry after a lost HTTP response.
            result = graph.get_state(config).values
            if not result.get("order"):
                raise HTTPException(409, "No order is awaiting confirmation.")
        else:
            result = graph.invoke(Command(resume=data.action == "confirm"), config)
        review = checkout_payload(graph, config)
        payment = result.get("payment") or {}
        if review:
            reply = "Your cart changed. Review the updated order and confirm again."
        elif result.get("error"):
            reply = "Checkout could not continue: " + result["error"]["error"].replace("_", " ")
        elif result.get("order"):
            reply = "Order " + result["order"]["order_id"] + " created. Payment is pending."
            if not payment.get("url"):
                reply += " The sandbox payment service is not configured. Contact LifeStore for assistance."
        else:
            reply = "Checkout cancelled. You can change your cart or continue shopping."
        return {"session_id": session_id, "reply": reply, "checkout": review,
                "payment_url": payment.get("url")}
    if review:
        return {"session_id": session_id, "reply": "Please confirm the reviewed order, or cancel to change it.",
                "checkout": review}
    result = chat_graph().invoke({"messages": [("user", data.message)]}, config)
    return {"reply": result["messages"][-1].text, "session_id": session_id,
            "checkout": checkout_payload(graph, config)}

@router.post('/api/chat')
def chat(data: ChatInput, request: Request, response: Response, authorization: str = Header(default='')):
    from app.api.auth import current_user, check_write
    from app.api.conversations import start_turn, finish_turn, detail
    response.headers['Cache-Control'] = 'no-store'
    token = os.getenv('CHAT_TEST_TOKEN', '')
    legacy = (os.getenv('CHAT_ALLOW_LEGACY') == '1' and bool(token)
              and hmac.compare_digest(authorization.encode(), ('Bearer ' + token).encode()))
    user = None if legacy else current_user(request)
    if user:
        check_write(request)
    if not data.action and not data.message.strip():
        raise HTTPException(422, 'Enter a question.')
    if data.action and not data.session_id:
        raise HTTPException(422, 'An existing checkout session is required.')
    if not busy.acquire(blocking=False):
        raise HTTPException(429, 'A reply is still being prepared. Try again shortly.')
    turn_started = False
    try:
        session_id = str(data.session_id or uuid4())
        if user:
            label = data.message if not data.action else ('Confirm order' if data.action == 'confirm' else 'Cancel checkout')
            session_id, previous = start_turn(str(data.session_id) if data.session_id else None,
                                             user, str(data.request_id), label)
            if previous is not None:
                return {**detail(session_id, user['id']), 'reply': previous}
            turn_started = True
        result = process_chat(data, session_id)
        if user:
            finish_turn(str(data.request_id), result['reply'])
            result.update(detail(session_id, user['id']))
        return result
    except HTTPException:
        if turn_started:
            finish_turn(str(data.request_id), 'This request could not be completed. Please try again.')
        raise
    except Exception as error:
        logger.warning('Chat failed: %s', type(error).__name__)
        if turn_started:
            finish_turn(str(data.request_id), 'The service could not reply. Please try again.')
        raise HTTPException(503, 'The AI service could not reply. Please try again shortly.') from None
    finally:
        busy.release()
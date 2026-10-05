"""Protected single-tester browsing interface; no checkout routing."""

import hmac
import logging
import os
from functools import lru_cache
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.graph.browsing import build_browsing_graph

router = APIRouter()
busy = Lock()
logger = logging.getLogger(__name__)


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: UUID | None = None


@lru_cache(maxsize=1)
def chat_graph():
    return build_browsing_graph()


@router.get("/", response_class=HTMLResponse)
def chat_page():
    return HTMLResponse(Path(__file__).with_name("chat.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/api/chat")
def chat(data: ChatInput, authorization: str = Header(default="")):
    token = os.getenv("CHAT_TEST_TOKEN", "")
    if not token or not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
        raise HTTPException(401, "Open your private test link to start chatting.")
    if not data.message.strip():
        raise HTTPException(422, "Enter a question.")
    if not busy.acquire(blocking=False):
        raise HTTPException(429, "A reply is still being prepared. Try again shortly.")
    session_id = str(data.session_id or uuid4())
    try:
        result = chat_graph().invoke({"messages": [("user", data.message)]},
                                    {"configurable": {"thread_id": session_id}, "recursion_limit": 16})
        return {"reply": result["messages"][-1].text, "session_id": session_id}
    except Exception as error:
        # Provider exceptions can contain prompts/credentials; log only the type.
        logger.warning("Chat failed: %s", type(error).__name__)
        raise HTTPException(503, "The AI service could not reply. Please try again shortly.") from None
    finally:
        busy.release()

"""Phase 0 application entry point."""

from fastapi import FastAPI
from app.api.payhere import router as payhere_router
from app.api.chat import router as chat_router

app = FastAPI(title="LifeStore Agent", version="0.1.0")
app.include_router(payhere_router)
app.include_router(chat_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

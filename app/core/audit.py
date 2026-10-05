"""Persistent audit records for tool calls."""
from datetime import datetime, timezone
from functools import lru_cache, wraps
from inspect import signature
from uuid import uuid4

from langchain_core.runnables.config import ensure_config
from sqlalchemy import DateTime, Index, JSON
from sqlalchemy.orm import Mapped, Session, mapped_column
from pydantic_core import to_jsonable_python
from app.db.models import Base
from app.db.session import get_engine


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_session_timestamp", "session_id", "timestamp"),)
    id: Mapped[str] = mapped_column(primary_key=True)
    session_id: Mapped[str]
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tool_name: Mapped[str]
    input: Mapped[dict] = mapped_column(JSON)
    output: Mapped[dict | list] = mapped_column(JSON)


@lru_cache(maxsize=1)
def get_audit_engine():
    return get_engine()


def audited(name=None):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            inputs = to_jsonable_python(dict(signature(function).bind(*args, **kwargs).arguments))
            nested = inputs.get("data", {})
            session_id = inputs.get("session_id") or nested.get("session_id") or ensure_config().get("configurable", {}).get("thread_id") or "unscoped"
            def save(output):
                with Session(get_audit_engine()) as session, session.begin():
                    session.add(AuditLog(id=uuid4().hex, session_id=str(session_id),
                        timestamp=datetime.now(timezone.utc), tool_name=name or function.__name__,
                        input=inputs, output=to_jsonable_python(output)))
            try:
                result = function(*args, **kwargs)
            except Exception as error:
                save({"error": "tool_exception", "type": type(error).__name__})
                raise
            save(result)
            return result
        return wrapped
    return decorate

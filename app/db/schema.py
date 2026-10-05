"""Initial schema creation; safe to rerun, but does not alter existing tables."""

from sqlalchemy import Engine

from app.db.models import Base
from app.core.audit import AuditLog  # Register audit_logs with Base.metadata.
from app.db.session import get_engine


def create_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def main() -> None:
    engine = get_engine()
    try:
        create_schema(engine)
        print("LifeStore schema ready")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

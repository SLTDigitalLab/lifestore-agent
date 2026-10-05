"""Database configuration shared by schema and seed commands."""

import os

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine


def get_engine() -> Engine:
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set DATABASE_URL in the environment or .env before accessing the database")
    return create_engine(url, pool_pre_ping=True)

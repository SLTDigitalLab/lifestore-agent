"""Process-owned PostgreSQL checkpoint pools, isolated by graph in the same DB."""

import atexit
from functools import lru_cache

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from langgraph.checkpoint.postgres import PostgresSaver

from app.db.session import get_engine


@lru_cache(maxsize=2)
def get_checkpointer(graph: str) -> PostgresSaver:
    if graph not in ("browsing", "checkout"):
        raise ValueError("Unknown graph")
    engine = get_engine()
    try:
        uri = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    finally:
        engine.dispose()
    schema = "lifestore_" + graph + "_checkpoints"
    with psycopg.connect(uri, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    pool = ConnectionPool(uri, min_size=1, max_size=5, kwargs={
        "autocommit": True, "row_factory": dict_row,
        "options": f"-c search_path={schema}",
    })
    try:
        pool.wait()
        saver = PostgresSaver(pool)
        saver.setup()
    except Exception:
        pool.close()
        raise
    atexit.register(pool.close)
    return saver

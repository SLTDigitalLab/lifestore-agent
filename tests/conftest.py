"""PostgreSQL integration tests isolated from application data by schema."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db.schema import create_schema


@pytest.fixture
def checkpoint_factory(db_engine):
    from contextlib import contextmanager
    import psycopg
    from psycopg.rows import dict_row
    from langgraph.checkpoint.postgres import PostgresSaver

    @contextmanager
    def connect():
        schema = db_engine.get_execution_options()["schema_translate_map"][None]
        uri = db_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
        with psycopg.connect(uri, autocommit=True, row_factory=dict_row,
                             options=f"-c search_path={schema}") as connection:
            saver = PostgresSaver(connection)
            saver.setup()
            yield saver
    return connect


@pytest.fixture
def pg_checkpointer(checkpoint_factory):
    with checkpoint_factory() as saver:
        yield saver


def pytest_addoption(parser):
    parser.addoption("--provider", action="append", choices=["gemini", "groq", "openai"],
                     help="Run the live 14-case gate for this provider; repeat for a matrix")
    parser.addoption("--run-live-llm", action="store_true", default=False,
                     help="Opt in to real Gemini/Groq API calls (requires both API keys)")


def pytest_configure(config):
    from dotenv import load_dotenv
    load_dotenv()
    required = {"gemini": ["GOOGLE_API_KEY"], "groq": ["GROQ_API_KEY"],
                "openai": ["OPENAI_API_KEY", "OPENAI_MODEL"]}
    if not config.getoption("collectonly"):
        missing = {key for provider in config.getoption("provider") or []
                   for key in required[provider] if not os.getenv(key, "").strip()}
        if missing:
            raise pytest.UsageError("Provider gate requires: " + ", ".join(sorted(missing)))


def pytest_generate_tests(metafunc):
    if "provider" in metafunc.fixturenames:
        selected = list(dict.fromkeys(metafunc.config.getoption("provider") or []))
        metafunc.parametrize("provider", selected or [pytest.param(None, marks=pytest.mark.skip(reason="Select --provider to run the live migration gate"))])


@pytest.fixture
def db_engine(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("Set TEST_DATABASE_URL to a PostgreSQL database (tests use an isolated schema)")
    engine = create_engine(url)
    schema = "test_lifestore_" + uuid4().hex
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    isolated = engine.execution_options(schema_translate_map={None: schema})
    from app.core import audit
    monkeypatch.setattr(audit, "get_audit_engine", lambda: isolated)
    try:
        create_schema(isolated)
        yield isolated
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()

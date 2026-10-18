import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from src.core.config import settings
from src.db.checkpoint import postgres_checkpointer
from src.main import app


def test_lifespan_without_database_url(monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", "")

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert getattr(app.state, "multi_agent_app", None) is None


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run the Postgres checkpoint round-trip",
)
def test_postgres_checkpointer_roundtrip():
    async def roundtrip():
        async with postgres_checkpointer(os.environ["TEST_DATABASE_URL"]) as saver:
            await saver.setup()
            assert saver is not None

    # psycopg's async driver cannot use Windows' default ProactorEventLoop.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(roundtrip())

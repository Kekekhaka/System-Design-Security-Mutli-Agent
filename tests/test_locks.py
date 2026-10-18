import asyncio
import os

import pytest

from src.core.config import settings
from src.db.locks import RunInProgress, exclusive_user_run


async def _second_run_is_rejected(scope: str) -> None:
    async with exclusive_user_run(scope, "alice"):
        with pytest.raises(RunInProgress):
            async with exclusive_user_run(scope, "alice"):
                pass
        # Other users are independent.
        async with exclusive_user_run(scope, "bob"):
            pass
    # Released after the first run finishes.
    async with exclusive_user_run(scope, "alice"):
        pass


def run_async(coro):
    # psycopg's async driver cannot use Windows' default ProactorEventLoop.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coro)


def test_in_process_lock(monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    run_async(_second_run_is_rejected("optimize-local"))


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run the Postgres advisory lock test",
)
def test_postgres_advisory_lock(monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    run_async(_second_run_is_rejected("optimize-pg"))

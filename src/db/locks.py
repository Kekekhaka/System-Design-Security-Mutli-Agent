import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager

from src.core.config import settings

_local_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class RunInProgress(Exception):
    """The user already has a run of this kind in flight."""


@asynccontextmanager
async def exclusive_user_run(scope: str, user_id: str):
    """Allow one `scope` run per user at a time; raise RunInProgress otherwise.

    With DATABASE_URL set this is a Postgres session advisory lock, so it holds
    across every worker and replica sharing the database (and is released if
    the connection drops). Without it, an in-process lock is the fallback.
    """
    key = f"{scope}:{user_id}"
    if not settings.DATABASE_URL:
        lock = _local_locks[key]
        if lock.locked():
            raise RunInProgress(key)
        async with lock:
            yield
        return

    import psycopg  # lazy: the offline test suite never needs the driver

    async with await psycopg.AsyncConnection.connect(
        settings.DATABASE_URL, autocommit=True
    ) as conn:
        cursor = await conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,)
        )
        (acquired,) = await cursor.fetchone()
        if not acquired:
            raise RunInProgress(key)
        try:
            yield
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))

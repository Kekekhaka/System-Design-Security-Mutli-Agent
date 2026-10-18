from contextlib import asynccontextmanager


@asynccontextmanager
async def postgres_checkpointer(database_url: str):
    """Yield an AsyncPostgresSaver for `database_url`.

    Imported lazily so environments without langgraph-checkpoint-postgres /
    psycopg (the offline test suite) never touch the driver. The saver owns a
    connection pool for the lifetime of the context; call `setup()` once on
    startup to create the checkpoint tables.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        yield saver

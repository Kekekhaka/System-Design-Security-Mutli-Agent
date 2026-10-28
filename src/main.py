import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.agents.multi_agent_graph import build_multi_agent
from src.api.routes import auth, project
from src.core.config import settings
from src.core.security import jwt_secret
from src.db.checkpoint import postgres_checkpointer

logger = logging.getLogger(__name__)


def production_config_problems() -> list[str]:
    """Settings that are fine on a laptop but unsafe in production."""
    problems = []
    if settings.ENABLE_DEV_AUTH:
        problems.append("ENABLE_DEV_AUTH must be false (it mints tokens without a password)")
    origins = {o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()}
    if not origins or "*" in origins:
        problems.append("CORS_ORIGINS must list explicit origins, not *")
    if not settings.DATABASE_URL:
        problems.append("DATABASE_URL must be set (sessions would be lost on restart)")
    if settings.HARNESS_MAX_MODEL_CALLS <= 0:
        problems.append(
            "HARNESS_MAX_MODEL_CALLS must be set (one /project/optimize request "
            "can otherwise make 160+ model calls)"
        )
    return problems


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate config, then attach a durable multi-agent app when DATABASE_URL is set."""
    jwt_secret()  # fail fast on a missing or placeholder signing secret
    if settings.APP_ENV.strip().lower() == "production":
        problems = production_config_problems()
        if problems:
            raise RuntimeError(
                "Refusing to start with APP_ENV=production: " + "; ".join(problems)
            )
    if settings.ENABLE_DEV_AUTH:
        logger.warning(
            "ENABLE_DEV_AUTH is on: /auth/token mints tokens for any username. "
            "Never enable this in production."
        )
    if not settings.OPENROUTER_FALLBACK_MODEL:
        logger.warning(
            "OPENROUTER_FALLBACK_MODEL is not set; OpenRouter rate limits or "
            "outages will fail requests instead of failing over."
        )

    if settings.DATABASE_URL:
        if sys.platform == "win32" and isinstance(
            asyncio.get_running_loop(), asyncio.ProactorEventLoop
        ):
            raise RuntimeError(
                "Postgres (psycopg async) cannot run on Windows' default "
                "ProactorEventLoop. Start with: "
                "uvicorn src.main:app --loop asyncio:SelectorEventLoop"
            )
        async with postgres_checkpointer(settings.DATABASE_URL) as saver:
            await saver.setup()
            app.state.multi_agent_app = build_multi_agent(checkpointer=saver)
            yield
        return

    if settings.REQUIRE_DATABASE:
        raise RuntimeError("REQUIRE_DATABASE is true but DATABASE_URL is not set.")
    logger.warning(
        "DATABASE_URL is not set; sessions are kept in memory and lost on restart."
    )
    yield


app = FastAPI(title=settings.APP_TITLE, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip() for origin in settings.CORS_ORIGINS.split(",") if origin.strip()
    ]
    or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(project.router)

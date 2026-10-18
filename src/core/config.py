from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "deepseek/deepseek-v4-flash"
    OPENROUTER_FALLBACK_MODEL: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    # Output budget per call, reasoning tokens included. DeepSeek V4 Flash can
    # reason past 4096 tokens and return no text at all, so keep this generous.
    LLM_MAX_TOKENS: int = 32000
    # Verify TLS with the OS certificate store (works behind corporate proxies).
    USE_OS_TRUST_STORE: bool = True
    APP_BASE_URL: str = "http://localhost:8000"
    APP_TITLE: str = "System Design & AppSec Agent"

    # "production" refuses to start with dev-only settings (see main.lifespan).
    APP_ENV: str = "development"

    # No usable default: startup fails until a real secret is configured.
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60
    # Passwordless token minting for local testing only; off unless opted in.
    ENABLE_DEV_AUTH: bool = False

    MAX_REVISIONS: int = 5
    # Non-streaming /project/design gives up after this; long runs use the stream.
    DESIGN_TIMEOUT_SECONDS: int = 120

    HARNESS_CANDIDATES: int = 3

    SECURITY_RAG_ENABLED: bool = True
    SECURITY_RAG_TOP_K: int = 3
    KNOWLEDGE_DIR: str = ""
    DESIGN_RAG_ENABLED: bool = True
    DESIGN_RAG_TOP_K: int = 3
    DESIGN_KNOWLEDGE_DIR: str = ""
    # "bm25" (offline, default) or "chroma" (needs requirements-chroma.txt)
    RAG_BACKEND: str = "bm25"
    CHROMA_DIR: str = ""

    CORS_ORIGINS: str = "*"

    DATABASE_URL: str = ""
    # Refuse to start without DATABASE_URL instead of falling back to memory.
    REQUIRE_DATABASE: bool = False

    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"


settings = Settings()

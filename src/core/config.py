from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "deepseek/deepseek-v4-flash"
    OPENROUTER_FALLBACK_MODEL: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    # Model clients refuse any base URL that is not openrouter.ai. Set true only to
    # deliberately allow another endpoint (for example a local model server).
    ALLOW_NON_OPENROUTER: bool = False
    # Output budget per call, reasoning tokens included. DeepSeek V4 Flash can
    # reason past 4096 tokens and return no text at all, so keep this generous.
    LLM_MAX_TOKENS: int = 32000
    # Reasoning effort for every model call that does not set its own. "" keeps the
    # model's default (slow and variable: tens of thousands of hidden tokens);
    # "none" turns reasoning off, the fast mode used for live demos.
    LLM_REASONING_EFFORT: str = ""
    # Hard deadline for any single model call (0 disables). The longest
    # legitimate call seen so far took about 8 minutes.
    LLM_CALL_TIMEOUT_SECONDS: int = 1200
    # The design-to-spec export is mechanical, so it runs with reasoning off and a
    # larger output budget for big specs. Measured on DeepSeek V4 Flash: "none"
    # used 0 reasoning tokens, while "low" and "minimal" used MORE than the
    # default. Empty string = the model's default.
    EXPORT_REASONING_EFFORT: str = "none"
    EXPORT_MAX_TOKENS: int = 64000
    # Verify TLS with the OS certificate store (works behind corporate proxies).
    USE_OS_TRUST_STORE: bool = True
    APP_BASE_URL: str = "http://localhost:8000"
    APP_TITLE: str = "Lekgotla"

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
    # Harness2 persona library. Empty paths resolve to data/harness/.
    HARNESS_STORE_PATH: str = ""
    HARNESS_HISTORY_PATH: str = ""
    # 0 means "use MAX_REVISIONS".
    HARNESS_MAX_REVISIONS: int = 0
    HARNESS_LIBRARY_CAP: int = 12
    # Reasoning is off by default for harness runs: a candidate must not spend
    # its whole output budget on hidden reasoning.
    HARNESS_REASONING_EFFORT: str = "none"
    # Opt in to the promoted champion persona in the team's audits.
    HARNESS_USE_CHAMPION: bool = False
    # HMAC key for the persona store and run history (empty = unsigned store).
    HARNESS_HMAC_KEY: str = ""
    # Hard per-run model-call cap. 0 means "estimate + 2".
    HARNESS_MAX_MODEL_CALLS: int = 0
    # Fixture calls spent proving the baseline auditor still works (2 fixtures).
    HARNESS_SANITY_FIXTURES: int = 1

    # Team build runs (scripts/run_team.py --build): the engineers and QA are
    # opencode runs in an isolated git worktree; the tests run there for real.
    OPENCODE_BIN: str = "opencode"
    # Every model call goes through OpenRouter, opencode included: the model must be
    # "openrouter/<vendor>/<model>". The old default, opencode's own free provider,
    # bypassed OpenRouter.
    OPENCODE_MODEL: str = "openrouter/deepseek/deepseek-v4-flash"
    # A DEDICATED OpenRouter key for opencode builds only (create one with a small credit
    # limit at openrouter.ai/settings/keys). It is passed to the opencode process alone;
    # git and the tests never get it, and the main OPENROUTER_API_KEY never reaches the
    # build. It must differ from OPENROUTER_API_KEY. Builds refuse to start without it.
    OPENCODE_OPENROUTER_API_KEY: str = ""
    ENGINEER_TIMEOUT_SECONDS: int = 1800
    TEAM_TEST_COMMAND: str = "python -m pytest -q"
    TEAM_TEST_TIMEOUT_SECONDS: int = 900
    # The diff reviewers see; longer diffs are cut with a note saying so.
    BUILD_DIFF_MAX_CHARS: int = 60000
    # Team runs: the longest a single model call may take (the shared default is 1200 s).
    # Real stages take 1-5 minutes; a call that stalls for 20 minutes only wastes the run,
    # and the team graph retries a stage once after a timeout (see run_team.py).
    TEAM_CALL_TIMEOUT_SECONDS: int = 600
    # Where build worktrees go. Must be outside every git repository (checked):
    # nested in one, opencode adopts that repository as its workspace.
    # Empty = <system temp>/team-worktrees.
    TEAM_WORKTREE_ROOT: str = ""

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

import logging
from functools import lru_cache

from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from src.core.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def use_os_trust_store() -> bool:
    """Verify TLS against the OS certificate store instead of certifi's bundle.

    Needed behind TLS-inspecting corporate proxies, whose root CA the OS
    trusts but certifi does not. Must run before HTTP clients are created.
    """
    if not settings.USE_OS_TRUST_STORE:
        return False
    try:
        import truststore
    except ImportError:
        return False
    truststore.inject_into_ssl()
    return True


class ModelOutputError(RuntimeError):
    """A model call returned no usable text."""


def checked_content(response, stage: str) -> str:
    """Return the response text, refusing empty or truncated output.

    Reasoning models can spend the whole token budget thinking and return no
    text (finish_reason "length"). Treating that as a normal reply is unsafe:
    an empty analysis reads as "no findings" and an empty design makes the
    supervisor re-run the designer indefinitely.
    """
    content = response.content if isinstance(response.content, str) else ""
    finish_reason = (getattr(response, "response_metadata", None) or {}).get("finish_reason")
    if not content.strip() or finish_reason == "length":
        raise ModelOutputError(
            f"The {stage} stage returned "
            f"{'truncated' if content.strip() else 'no'} output "
            f"(finish_reason={finish_reason!r}). Raise LLM_MAX_TOKENS "
            f"(currently {settings.LLM_MAX_TOKENS}) or retry."
        )
    return content


def invoke_checked(llm, messages, stage: str, attempts: int = 2) -> str:
    """Invoke and return checked text, retrying once on empty or truncated output."""
    for attempt in range(1, attempts + 1):
        try:
            return checked_content(llm.invoke(messages), stage)
        except ModelOutputError:
            if attempt == attempts:
                raise
            logger.warning("%s stage returned unusable output; retrying (%d/%d)", stage, attempt, attempts)
    raise AssertionError("unreachable")


def _chat_model(model: str, temperature: float) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        api_key=settings.OPENROUTER_API_KEY,
        base_url=settings.OPENROUTER_BASE_URL,
        temperature=temperature,
        max_tokens=settings.LLM_MAX_TOKENS,
        default_headers={
            "HTTP-Referer": settings.APP_BASE_URL,
            "X-Title": settings.APP_TITLE,
        },
    )


def get_llm(temperature: float = 0.1, model: str | None = None) -> Runnable:
    """Return a DeepSeek V4 Flash client via OpenRouter.

    When OPENROUTER_FALLBACK_MODEL is set, transient failures or rate limits on
    the primary model fall through to the fallback model on the same provider.
    """
    if not settings.OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    use_os_trust_store()
    primary_model = model or settings.OPENROUTER_MODEL
    primary = _chat_model(primary_model, temperature)
    fallback_model = settings.OPENROUTER_FALLBACK_MODEL
    if fallback_model and fallback_model != primary_model:
        return primary.with_fallbacks([_chat_model(fallback_model, temperature)])
    return primary

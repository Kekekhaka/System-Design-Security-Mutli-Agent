import contextvars
import logging
import threading
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


class ModelCallTimeout(RuntimeError):
    """A model call did not finish within LLM_CALL_TIMEOUT_SECONDS."""


def call_with_deadline(fn, stage: str, timeout: float | None = None):
    """Run `fn()` and return its result, or raise ModelCallTimeout after `timeout` seconds.

    The HTTP client's own timeouts do not bound a whole call: a provider that keeps
    the connection alive with keep-alive bytes can stall a request indefinitely
    (seen live: a call still pending after six hours). `fn` runs in a daemon thread
    with the caller's context copied, so LangGraph callbacks and streaming events
    keep working. On timeout the thread is abandoned, not killed; it ends whenever
    the request does. A `timeout` of 0 or less disables the deadline.
    """
    seconds = settings.LLM_CALL_TIMEOUT_SECONDS if timeout is None else timeout
    if not seconds or seconds <= 0:
        return fn()

    outcome: dict = {}
    context = contextvars.copy_context()

    def target() -> None:
        try:
            outcome["value"] = context.run(fn)
        except BaseException as exc:  # re-raised in the caller below
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True, name=f"llm-{stage}")
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise ModelCallTimeout(
            f"The {stage} stage did not finish within {seconds:g}s. Raise "
            "LLM_CALL_TIMEOUT_SECONDS if the output is legitimately that long, "
            "otherwise retry."
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


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
            f"(finish_reason={finish_reason!r}, {_usage_summary(response)}). Raise the "
            "output token limit (LLM_MAX_TOKENS, or EXPORT_MAX_TOKENS for the export) "
            "or retry."
        )
    return content


def _usage_summary(response) -> str:
    """Tokens spent, so an empty reply can be told apart: output equal to
    reasoning means the budget went on thinking; output near zero is a provider
    hiccup."""
    usage = getattr(response, "usage_metadata", None) or {}
    reasoning = (usage.get("output_token_details") or {}).get("reasoning")
    return f"output_tokens={usage.get('output_tokens')}, reasoning_tokens={reasoning}"


def invoke_checked(llm, messages, stage: str, attempts: int = 3) -> str:
    """Invoke under a deadline and return checked text.

    Empty or truncated output is retried up to twice (seen live: three empty
    replies in a row across two stages of one audit). A timeout is not retried:
    the call already used its whole time budget.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = call_with_deadline(lambda: llm.invoke(messages), stage)
            return checked_content(response, stage)
        except ModelOutputError:
            if attempt == attempts:
                raise
            logger.warning("%s stage returned unusable output; retrying (%d/%d)", stage, attempt, attempts)
    raise AssertionError("unreachable")


def _chat_model(
    model: str,
    temperature: float,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> ChatOpenAI:
    extra = {"extra_body": {"reasoning": {"effort": reasoning_effort}}} if reasoning_effort else {}
    return ChatOpenAI(
        model=model,
        api_key=settings.OPENROUTER_API_KEY,
        base_url=settings.OPENROUTER_BASE_URL,
        temperature=temperature,
        max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
        default_headers={
            "HTTP-Referer": settings.APP_BASE_URL,
            "X-Title": settings.APP_TITLE,
        },
        **extra,
    )


def get_llm(
    temperature: float = 0.1,
    model: str | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> Runnable:
    """Return a DeepSeek V4 Flash client via OpenRouter.

    When OPENROUTER_FALLBACK_MODEL is set, transient failures or rate limits on
    the primary model fall through to the fallback model on the same provider.
    `max_tokens` overrides LLM_MAX_TOKENS, and `reasoning_effort` ("low", "medium",
    "high", ...) sets OpenRouter's reasoning effort for both models.
    """
    if not settings.OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    use_os_trust_store()
    # An explicit argument wins; otherwise the global LLM_REASONING_EFFORT applies.
    effort = reasoning_effort if reasoning_effort is not None else (settings.LLM_REASONING_EFFORT or None)
    primary_model = model or settings.OPENROUTER_MODEL
    primary = _chat_model(primary_model, temperature, max_tokens, effort)
    fallback_model = settings.OPENROUTER_FALLBACK_MODEL
    if fallback_model and fallback_model != primary_model:
        return primary.with_fallbacks(
            [_chat_model(fallback_model, temperature, max_tokens, effort)]
        )
    return primary

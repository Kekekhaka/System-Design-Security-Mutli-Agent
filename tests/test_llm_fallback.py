import pytest
from langchain_core.runnables.fallbacks import RunnableWithFallbacks
from langchain_openai import ChatOpenAI

from src.core import llm as llm_module
from src.core.config import settings


def test_get_llm_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
    with pytest.raises(RuntimeError):
        llm_module.get_llm()


def test_get_llm_plain_without_fallback(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "dummy-key")
    monkeypatch.setattr(settings, "OPENROUTER_FALLBACK_MODEL", "")
    client = llm_module.get_llm()
    assert isinstance(client, ChatOpenAI)


def test_get_llm_wraps_fallback(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "dummy-key")
    monkeypatch.setattr(settings, "OPENROUTER_FALLBACK_MODEL", "fallback/model")
    client = llm_module.get_llm()
    assert isinstance(client, RunnableWithFallbacks)
    assert client.fallbacks[0].model_name == "fallback/model"


def test_fallback_client_supports_structured_output(monkeypatch):
    from src.agents.security.report import SecurityReport

    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "dummy-key")
    monkeypatch.setattr(settings, "OPENROUTER_FALLBACK_MODEL", "fallback/model")
    client = llm_module.get_llm()
    structured = client.with_structured_output(
        SecurityReport, method="function_calling", include_raw=True
    )
    assert structured is not None

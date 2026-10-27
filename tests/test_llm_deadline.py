import contextvars
import threading
import time

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables.fallbacks import RunnableWithFallbacks
from langchain_openai import ChatOpenAI

from src.agents.nodes import make_security_node
from src.core import llm as llm_module
from src.core.config import settings
from src.core.llm import ModelCallTimeout, ModelOutputError, call_with_deadline, invoke_checked
from tests.fakes import ScriptedLLM

CURRENT = contextvars.ContextVar("current", default="unset")


def test_returns_the_value_and_propagates_errors():
    assert call_with_deadline(lambda: 42, "t", timeout=5) == 42
    with pytest.raises(ValueError, match="boom"):
        call_with_deadline(lambda: (_ for _ in ()).throw(ValueError("boom")), "t", timeout=5)


def test_times_out_instead_of_waiting_forever():
    release = threading.Event()
    started = time.time()
    try:
        with pytest.raises(ModelCallTimeout, match="stuck stage"):
            call_with_deadline(lambda: release.wait(30), "stuck stage", timeout=0.1)
        assert time.time() - started < 5
    finally:
        release.set()


def test_deadline_disabled_when_zero():
    assert call_with_deadline(lambda: "ok", "t", timeout=0) == "ok"


def test_caller_context_is_visible_inside_the_call():
    # LangGraph passes its callbacks and streaming config through contextvars.
    CURRENT.set("graph-config")
    assert call_with_deadline(lambda: CURRENT.get(), "t", timeout=5) == "graph-config"


class BlockingLLM:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls = 0

    def invoke(self, messages, **kwargs):
        self.calls += 1
        self.release.wait(30)
        return AIMessage(content="late")


def test_invoke_checked_times_out_without_retrying(monkeypatch):
    monkeypatch.setattr(settings, "LLM_CALL_TIMEOUT_SECONDS", 0.1)
    llm = BlockingLLM()
    try:
        with pytest.raises(ModelCallTimeout):
            invoke_checked(llm, [], "designer")
        assert llm.calls == 1  # a timeout already used the whole budget
    finally:
        llm.release.set()


def test_empty_output_is_still_retried_once():
    llm = ScriptedLLM(["", "second try"])
    assert invoke_checked(llm, [], "designer") == "second try"
    with pytest.raises(ModelOutputError):
        invoke_checked(ScriptedLLM([""]), [], "designer")


def test_security_validation_stage_has_a_deadline(monkeypatch):
    release = threading.Event()

    class StuckValidatorLLM(ScriptedLLM):
        def with_structured_output(self, schema, **kwargs):
            class Stuck:
                def invoke(self, messages, **kw):
                    release.wait(30)

            return Stuck()

    node = make_security_node(StuckValidatorLLM(["scope", "draft"]))
    monkeypatch.setattr(settings, "LLM_CALL_TIMEOUT_SECONDS", 0.1)
    try:
        with pytest.raises(ModelCallTimeout, match="security validation"):
            node({"architecture": "arch", "api_spec": "spec", "revision_count": 0})
    finally:
        release.set()


def test_get_llm_applies_reasoning_and_token_overrides(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "dummy-key")
    monkeypatch.setattr(settings, "OPENROUTER_FALLBACK_MODEL", "fallback/model")
    client = llm_module.get_llm(max_tokens=64000, reasoning_effort="low")
    assert isinstance(client, RunnableWithFallbacks)
    for model in (client.runnable, *client.fallbacks):
        assert model.max_tokens == 64000
        assert model.extra_body == {"reasoning": {"effort": "low"}}


def test_get_llm_defaults_are_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "dummy-key")
    monkeypatch.setattr(settings, "OPENROUTER_FALLBACK_MODEL", "")
    client = llm_module.get_llm()
    assert isinstance(client, ChatOpenAI)
    assert client.max_tokens == settings.LLM_MAX_TOKENS
    assert not client.extra_body


def test_export_defaults_turn_reasoning_off():
    from src.core.config import Settings

    # Measured live: effort "none" used 0 reasoning tokens; "low" used more than default.
    assert Settings.model_fields["EXPORT_REASONING_EFFORT"].default == "none"
    assert Settings.model_fields["EXPORT_MAX_TOKENS"].default >= 64000
    assert Settings.model_fields["LLM_CALL_TIMEOUT_SECONDS"].default == 1200


def test_empty_output_error_reports_token_usage():
    empty = AIMessage(
        content="",
        response_metadata={"finish_reason": "stop"},
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 18466,
            "total_tokens": 18476,
            "output_token_details": {"reasoning": 18466},
        },
    )
    with pytest.raises(ModelOutputError, match=r"output_tokens=18466, reasoning_tokens=18466"):
        llm_module.checked_content(empty, "security analysis")


def test_a_stage_gets_two_retries_after_empty_replies():
    llm = ScriptedLLM(["", "", "third time"])
    assert invoke_checked(llm, [], "security analysis") == "third time"
    assert len(llm.calls) == 3
    with pytest.raises(ModelOutputError):
        invoke_checked(ScriptedLLM(["", "", ""]), [], "security analysis")


def test_global_reasoning_effort_applies_when_not_set_per_call(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "dummy-key")
    monkeypatch.setattr(settings, "OPENROUTER_FALLBACK_MODEL", "fallback/model")
    monkeypatch.setattr(settings, "LLM_REASONING_EFFORT", "none")

    client = llm_module.get_llm()
    for model in (client.runnable, *client.fallbacks):
        assert model.extra_body == {"reasoning": {"effort": "none"}}
    # An explicit argument still wins.
    explicit = llm_module.get_llm(reasoning_effort="high")
    assert explicit.runnable.extra_body == {"reasoning": {"effort": "high"}}


def test_no_global_reasoning_effort_leaves_the_model_default(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "dummy-key")
    monkeypatch.setattr(settings, "OPENROUTER_FALLBACK_MODEL", "")
    monkeypatch.setattr(settings, "LLM_REASONING_EFFORT", "")
    assert not llm_module.get_llm().extra_body


def test_deadline_follows_the_wall_clock_not_one_long_join(monkeypatch):
    # The deadline is polled against time.time(), so a clock that jumps forward (sleep,
    # resume) ends the wait at once instead of waiting for a paused timer.
    release = threading.Event()
    real_time = time.time
    jumped = {"by": 0.0}
    monkeypatch.setattr(llm_module.time, "time", lambda: real_time() + jumped["by"])

    def jump_later():
        time.sleep(0.2)
        jumped["by"] = 3600.0

    threading.Thread(target=jump_later, daemon=True).start()
    started = real_time()
    try:
        with pytest.raises(ModelCallTimeout):
            call_with_deadline(lambda: release.wait(30), "stuck stage", timeout=600)
        assert real_time() - started < 10
    finally:
        release.set()

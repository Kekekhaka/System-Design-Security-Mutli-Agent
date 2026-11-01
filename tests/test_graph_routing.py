from src.agents.multi_agent_graph import build_multi_agent, initial_state
from src.agents.nodes import is_approved, parse_design_sections
from src.agents.prompts import DESIGNER_SYSTEM_PROMPT
from src.core.config import settings
from tests.fakes import ScriptedLLM


DESIGN_V1 = (
    "===ARCHITECTURE===\n"
    "Architecture v1: POST /pay accepts raw card numbers and forwards them to the backend.\n"
    "===API SPEC===\n"
    "POST /pay {card_number, expiry, cvv, amount}"
)

DESIGN_V2 = (
    "===ARCHITECTURE===\n"
    "Architecture v2: card data goes straight to the payment provider; only tokens reach the backend.\n"
    "===API SPEC===\n"
    "POST /pay {payment_token, amount}"
)

SCOPE_NOTES = "Scope: card flow, gateway logging, ownership checks on writes."

DRAFT_REJECT = (
    "Draft findings: raw card numbers reach the backend; writes lack ownership checks."
)

DRAFT_APPROVE = "Draft findings: card data never reaches the backend; scoping checks present."

SECURITY_REJECT = (
    "CHANGES REQUESTED\n"
    "- Severity: Critical | Category: Sensitive data handling | Problem: raw card numbers "
    "reach the backend. Why it matters: PCI-DSS violation. Fix: tokenize via the payment provider."
)

SECURITY_APPROVE = (
    "APPROVED\nOnly payment tokens flow through the backend; card data never touches it."
)

REJECT_ROUND = [SCOPE_NOTES, DRAFT_REJECT, SECURITY_REJECT]
APPROVE_ROUND = [SCOPE_NOTES, DRAFT_APPROVE, SECURITY_APPROVE]


def run(requirements: str, responses: list[str]) -> tuple[dict, ScriptedLLM]:
    llm = ScriptedLLM(responses)
    graph = build_multi_agent(llm=llm)
    result = graph.invoke(initial_state(requirements))
    return result, llm


def designer_calls(llm: ScriptedLLM) -> list[list]:
    return [call for call in llm.calls if call[0].content == DESIGNER_SYSTEM_PROMPT]


def test_reject_then_redesign_then_approve():
    result, _ = run(
        "billing api",
        [DESIGN_V1, *REJECT_ROUND, DESIGN_V2, *APPROVE_ROUND],
    )

    assert "v2" in result["architecture"]
    assert "token" in result["api_spec"]
    assert is_approved(result["security_findings"])
    assert result["revision_count"] == 2
    assert result["next_agent"] == "FINISH"


def test_designer_receives_security_feedback():
    _, llm = run("billing api", [DESIGN_V1, *REJECT_ROUND, DESIGN_V2, *APPROVE_ROUND])

    second_designer_prompt = designer_calls(llm)[1][1].content
    assert "CHANGES REQUESTED" in second_designer_prompt
    assert "tokenize via the payment provider" in second_designer_prompt


def test_security_audit_runs_scope_analyze_validate():
    _, llm = run("billing api", [DESIGN_V1, *REJECT_ROUND, DESIGN_V2, *APPROVE_ROUND])

    assert llm.calls[0][0].content == DESIGNER_SYSTEM_PROMPT
    assert llm.calls[1][0].content.startswith("You are the scoping stage")
    assert llm.calls[2][0].content.startswith("You are the AppSec Architect")
    assert llm.calls[3][0].content.startswith("You are the validation stage")


def test_revision_cap_stops_the_loop(monkeypatch):
    monkeypatch.setattr(settings, "STOP_WHEN_NOT_IMPROVING", False)
    result, _ = run("billing api", [DESIGN_V1, *REJECT_ROUND])

    assert result["revision_count"] == settings.MAX_REVISIONS
    assert not is_approved(result["security_findings"])
    assert result["next_agent"] == "FINISH"
    assert result["stop_reason"] is None


def test_a_revision_that_fixes_nothing_ends_the_loop_early(monkeypatch):
    monkeypatch.setattr(settings, "MAX_REVISIONS", 5)
    # The same Critical finding every round: round 2 is no better than round 1.
    result, llm = run("billing api", [DESIGN_V1, *REJECT_ROUND])

    assert result["revision_count"] == 2
    assert result["blocking_history"] == [1, 1]
    assert result["stop_reason"] == "no_progress"
    assert not is_approved(result["security_findings"])
    assert len(designer_calls(llm)) == 2


def test_not_improving_compares_only_parsed_blocking_rounds():
    from src.agents.nodes import not_improving

    assert not not_improving([])
    assert not not_improving([3])
    assert not_improving([2, 2])
    assert not_improving([1, 3])
    assert not not_improving([3, 2])  # fewer blocking findings: keep going
    assert not not_improving([0, 0])  # nothing blocking: the cap or approval decides
    assert not not_improving([None, 2])  # unparsed report says nothing about the design
    assert not not_improving([2, None])
    assert not_improving([4, 2, 2])  # only the last two rounds count


def test_supervisor_stops_on_no_progress_only_when_enabled(monkeypatch):
    from src.agents.nodes import supervisor_node

    monkeypatch.setattr(settings, "MAX_REVISIONS", 5)
    stuck = {
        "architecture": "a",
        "security_findings": "CHANGES REQUESTED\n1. [High] x",
        "revision_count": 2,
        "blocking_history": [1, 1],
    }
    assert supervisor_node(stuck) == {"next_agent": "FINISH", "stop_reason": "no_progress"}
    improving = {**stuck, "blocking_history": [3, 1]}
    assert supervisor_node(improving) == {"next_agent": "designer"}

    monkeypatch.setattr(settings, "STOP_WHEN_NOT_IMPROVING", False)
    assert supervisor_node(stuck) == {"next_agent": "designer"}


def test_security_node_records_blocking_counts():
    from src.agents.nodes import make_security_node

    node = make_security_node(ScriptedLLM(REJECT_ROUND))
    first = node({"architecture": "a", "api_spec": "s", "revision_count": 0})
    assert first["blocking_history"] == [1]
    second = node({"architecture": "a", "api_spec": "s", "revision_count": 1, **first})
    assert second["blocking_history"] == [1, 1]


def test_settings_repr_hides_secrets(monkeypatch):
    for name in ("OPENROUTER_API_KEY", "JWT_SECRET", "HARNESS_HMAC_KEY",
                 "OPENCODE_OPENROUTER_API_KEY", "DATABASE_URL", "LANGFUSE_SECRET_KEY"):
        monkeypatch.setattr(settings, name, "sk-should-never-print")
        assert name not in repr(settings)
    assert "sk-should-never-print" not in repr(settings)
    assert "MAX_REVISIONS" in repr(settings)


def test_approval_check():
    assert is_approved("APPROVED - fine")
    assert is_approved("  approved \nnotes")
    assert not is_approved("CHANGES REQUESTED: not approved yet")
    assert not is_approved("")
    assert not is_approved(None)


def test_parse_design_sections():
    architecture, api_spec = parse_design_sections(DESIGN_V1)
    assert architecture.startswith("Architecture v1")
    assert api_spec.startswith("POST /pay")

    architecture, api_spec = parse_design_sections("no markers here")
    assert architecture == "no markers here"
    assert api_spec == ""


def test_empty_designer_output_raises_instead_of_looping():
    import pytest

    from src.core.llm import ModelOutputError

    with pytest.raises(ModelOutputError, match="designer"):
        run("billing api", [""])


def test_empty_security_analysis_fails_instead_of_approving():
    import pytest

    from src.core.llm import ModelOutputError

    # Live failure mode: analysis returns nothing (twice, so the retry also
    # fails) and validation would otherwise see "no findings" and approve.
    with pytest.raises(ModelOutputError, match="security analysis"):
        run("billing api", [DESIGN_V2, SCOPE_NOTES, "", ""])


def test_truncated_output_is_rejected():
    import pytest
    from langchain_core.messages import AIMessage

    from src.core.llm import ModelOutputError, checked_content

    truncated = AIMessage(content="===ARCHITECTURE===\npartial", response_metadata={"finish_reason": "length"})
    with pytest.raises(ModelOutputError, match="truncated"):
        checked_content(truncated, "designer")
    complete = AIMessage(content="ok", response_metadata={"finish_reason": "stop"})
    assert checked_content(complete, "designer") == "ok"


def test_single_empty_output_is_retried():
    result, llm = run(
        "billing api", ["", DESIGN_V2, SCOPE_NOTES, "", DRAFT_APPROVE, SECURITY_APPROVE]
    )

    assert "v2" in result["architecture"]
    assert is_approved(result["security_findings"])
    assert result["revision_count"] == 1
    assert len(llm.calls) == 6


def test_truncated_validator_output_names_the_cause():
    from langchain_core.messages import AIMessage

    from src.agents.nodes import make_security_node

    class TruncatedValidatorLLM(ScriptedLLM):
        def with_structured_output(self, schema, **kwargs):
            class Truncated:
                def invoke(self, messages, **kwargs):
                    raw = AIMessage(content="", response_metadata={"finish_reason": "length"})
                    return {"raw": raw, "parsed": None, "parsing_error": None}

            return Truncated()

    node = make_security_node(TruncatedValidatorLLM(["scope", "draft"]))
    result = node({"architecture": "arch", "api_spec": "spec", "revision_count": 0})

    assert not is_approved(result["security_findings"])
    assert "truncated" in result["security_findings"]
    assert "LLM_MAX_TOKENS" in result["security_findings"]


def test_a_per_run_revision_cap_stops_early_but_never_raises_the_limit(monkeypatch):
    from src.agents.nodes import supervisor_node
    from src.core.config import settings

    monkeypatch.setattr(settings, "MAX_REVISIONS", 3)
    rejected = {"architecture": "a", "security_findings": "CHANGES REQUESTED - x"}
    assert supervisor_node({**rejected, "revision_count": 1, "max_revisions": 1})["next_agent"] == "FINISH"
    assert supervisor_node({**rejected, "revision_count": 1, "max_revisions": None})["next_agent"] == "designer"
    assert supervisor_node({**rejected, "revision_count": 3, "max_revisions": 10})["next_agent"] == "FINISH"

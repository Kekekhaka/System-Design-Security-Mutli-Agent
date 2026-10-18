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


def test_revision_cap_stops_the_loop():
    result, _ = run("billing api", [DESIGN_V1, *REJECT_ROUND])

    assert result["revision_count"] == settings.MAX_REVISIONS
    assert not is_approved(result["security_findings"])
    assert result["next_agent"] == "FINISH"


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

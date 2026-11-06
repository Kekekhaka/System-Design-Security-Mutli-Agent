import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from src.agents.security.prompts import SECURITY_SYSTEM_PROMPT, with_review_emphasis
from src.core.config import settings
from src.harness.optimizer import (
    FALLBACK_CANDIDATES,
    build_harness_optimizer,
    parse_candidates,
    pick_winner,
)
from tests.fakes import ScriptedLLM, StructuredFake, report_from_text

CANDIDATES = ["Strict compliance persona.", "Red-team persona.", "Lenient persona."]

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


class BaselineJudgeLLM:
    """Order-independent judge (probes run in parallel): rejects raw card data."""

    def invoke(self, messages, **kwargs) -> AIMessage:
        if "Draft findings from the analysis stage" not in messages[-1].content:
            return AIMessage(content="judge notes")
        if "raw card numbers" in messages[-1].content:
            return AIMessage(content=SECURITY_REJECT)
        return AIMessage(content=SECURITY_APPROVE)

    def with_structured_output(self, schema, **kwargs) -> StructuredFake:
        return StructuredFake(self, report_from_text, kwargs.get("include_raw", False))


class ExplodingLLM:
    def invoke(self, messages, **kwargs):
        raise RuntimeError("provider down")

    def with_structured_output(self, schema, **kwargs):
        return self


def optimizer_for(monkeypatch, agent_llms: list, judge=None):
    monkeypatch.setattr(settings, "HARNESS_CANDIDATES", len(agent_llms))
    pending = list(agent_llms)
    return build_harness_optimizer(
        llm=ScriptedLLM([json.dumps(CANDIDATES[: len(agent_llms)])]),
        agent_llm_factory=lambda: pending.pop(0),
        judge_llm=judge or BaselineJudgeLLM(),
    )


def test_parse_candidates_extracts_fenced_json():
    content = 'Here you go:\n```json\n["a", "b", "c"]\n```\nEnjoy.'
    assert parse_candidates(content, 3) == ["a", "b", "c"]


def test_parse_candidates_falls_back_on_garbage():
    assert parse_candidates("not json at all", 3) == FALLBACK_CANDIDATES[:3]


def test_parse_candidates_pads_and_dedupes():
    prompts = parse_candidates('["keep", "keep"]', 3)
    assert prompts[0] == "keep"
    assert len(prompts) == 3
    assert len(set(prompts)) == 3


def test_pick_winner_prefers_approved_then_fewest_revisions():
    results = [
        {"approved": False, "revisions": 3, "findings": "x" * 100},
        {"approved": True, "revisions": 2, "findings": "APPROVED"},
        {"approved": True, "revisions": 1, "findings": "APPROVED"},
    ]
    assert pick_winner(results) is results[2]


def test_review_emphasis_keeps_baseline_rules():
    prompt = with_review_emphasis("Red-team persona.")
    assert prompt.startswith(SECURITY_SYSTEM_PROMPT)
    assert prompt.rstrip().endswith("Red-team persona.")


def test_lenient_persona_cannot_win_by_approving_a_weak_design(monkeypatch):
    strict = ScriptedLLM([DESIGN_V1, *REJECT_ROUND])
    redteam = ScriptedLLM([DESIGN_V1, *REJECT_ROUND, DESIGN_V2, *APPROVE_ROUND])
    # Approves the raw-card design on its own audit after a single revision.
    lenient = ScriptedLLM([DESIGN_V1, *APPROVE_ROUND])

    result = asyncio.run(optimizer_for(monkeypatch, [strict, redteam, lenient]).ainvoke({"task": "billing api"}))

    assert result["winning_prompt"] == CANDIDATES[1]
    # strict repeats the same Critical finding, so its loop stops on no progress
    # after round 3 instead of spending every round up to MAX_REVISIONS.
    assert [probe["revisions"] for probe in result["probe_results"]] == [3, 2, 1]
    assert [probe["approved"] for probe in result["probe_results"]] == [False, True, False]
    assert any(
        call[0].content == with_review_emphasis(CANDIDATES[0]) for call in strict.calls
    )


def test_failed_probe_is_skipped(monkeypatch):
    redteam = ScriptedLLM([DESIGN_V1, *REJECT_ROUND, DESIGN_V2, *APPROVE_ROUND])

    result = asyncio.run(optimizer_for(monkeypatch, [ExplodingLLM(), redteam]).ainvoke({"task": "billing api"}))

    assert [probe["prompt"] for probe in result["probe_results"]] == [CANDIDATES[1]]
    assert result["winning_prompt"] == CANDIDATES[1]


def test_all_probes_failing_raises(monkeypatch):
    with pytest.raises(RuntimeError, match="All harness probes failed"):
        asyncio.run(
            optimizer_for(monkeypatch, [ExplodingLLM(), ExplodingLLM()]).ainvoke({"task": "billing api"})
        )

import asyncio
import json
import logging
import random
import re
from functools import lru_cache
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from src.agents.multi_agent_graph import build_multi_agent, initial_state
from src.agents.nodes import is_approved
from src.agents.security.graph import build_security_subgraph
from src.agents.security.prompts import with_review_emphasis
from src.core.config import settings
from src.core.llm import ModelCallTimeout, ModelOutputError, call_with_deadline, get_llm
from src.harness.guard import critical_rule_guard
from src.harness.scoring import (  # noqa: F401  (pick_winner/score_result are re-exported)
    pick_winner,
    score_result,
    weighted_findings,
)
from src.harness.seeds import FALLBACK_CANDIDATES, persona_id
from src.knowledge.retriever import load_default_retriever

logger = logging.getLogger(__name__)

PROPOSE_SYSTEM_PROMPT = (
    "You are an AI prompt optimizer for a system design review board. "
    "Output only a valid JSON array of strings in English."
)


class AllCandidatesFailed(RuntimeError):
    """Every candidate failed twice. The failures travel with the error."""

    def __init__(self, failures: list[dict]):
        super().__init__("All harness probes failed; see logs for details.")
        self.failures = failures


class HarnessState(TypedDict, total=False):
    task: str
    candidate_prompts: list[str]
    candidate_personas: list[dict]
    probe_results: list[dict]
    failures: list[dict]
    winning_prompt: str


SECURITY_AUDIT_CALLS = 3  # scope, analyze, validate


def harness_max_revisions() -> int:
    """HARNESS_MAX_REVISIONS (0 = MAX_REVISIONS), never above MAX_REVISIONS."""
    wanted = settings.HARNESS_MAX_REVISIONS or settings.MAX_REVISIONS
    return max(0, min(wanted, settings.MAX_REVISIONS))


def build_propose_prompt(task: str, count: int) -> str:
    return (
        f'We need to test different security reviewer personas for this task: "{task}"\n'
        f"Generate {count} distinct system prompts for the Security Auditor agent, each "
        "with a different review emphasis. Angles worth covering: strict compliance "
        "(OWASP / PCI-DSS), red-team (business-logic abuse and attack paths), cloud "
        "architecture (IAM, network boundaries, multi-tenancy), privacy and data "
        "protection, availability and abuse controls.\n"
        "Focus each prompt on the persona and what it prioritises; each one is layered "
        "on top of a baseline AppSec prompt that already defines the review rules and "
        "output protocol, so do not include output formatting rules.\n"
        f"Return exactly a JSON array of {count} strings."
    )


def parse_candidates(content: str, count: int) -> list[str]:
    """Extract up to `count` unique prompt strings, padding with fallbacks."""
    text = (content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    parsed = None
    try:
        start, end = text.index("["), text.rindex("]")
        parsed = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        parsed = None

    prompts: list[str] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str) and item.strip() and item.strip() not in prompts:
                prompts.append(item.strip())
    for fallback in FALLBACK_CANDIDATES:
        if len(prompts) >= count:
            break
        if fallback not in prompts:
            prompts.append(fallback)
    return prompts[:count]


def make_propose_node(llm):
    def propose_node(state: HarnessState) -> dict:
        # A caller that selected from the library passes the candidates in.
        if state.get("candidate_prompts"):
            return {"candidate_prompts": state["candidate_prompts"]}
        messages = [
            SystemMessage(content=PROPOSE_SYSTEM_PROMPT),
            HumanMessage(
                content=build_propose_prompt(state["task"], settings.HARNESS_CANDIDATES)
            ),
        ]
        response = call_with_deadline(lambda: llm.invoke(messages), "harness propose")
        return {
            "candidate_prompts": parse_candidates(
                response.content, settings.HARNESS_CANDIDATES
            )
        }

    return propose_node


def _failure_reason(exc: BaseException) -> str:
    text = str(exc).lower()
    if isinstance(exc, ModelCallTimeout) or "timed out" in text or "timeout" in text:
        return "timeout"
    if (
        isinstance(exc, ModelOutputError)
        or "token" in text
        or "truncated" in text
        or "no output" in text
    ):
        return "token_limit"
    return "provider_error"


def make_probe_node(agent_llm_factory, judge, max_revisions: int | None = None):
    """Run every candidate in parallel, retry a failure once, score the rest.

    Candidates only add emphasis on top of SECURITY_SYSTEM_PROMPT, and approval
    comes from one fixed baseline auditor rather than the candidate's own
    verdict, so a lenient persona cannot win by approving weak designs. A
    candidate that fails twice is recorded and the run continues with the
    others.
    """

    async def probe_node(state: HarnessState) -> dict:
        personas = state.get("candidate_personas") or [
            {"id": persona_id(prompt), "prompt": prompt}
            for prompt in state.get("candidate_prompts", [])
        ]
        probes = []
        for persona in personas:
            graph = build_multi_agent(
                llm=agent_llm_factory(),
                security_prompt=with_review_emphasis(persona["prompt"]),
            )
            probes.append((persona, graph))

        async def run_candidate(persona: dict, graph) -> dict:
            last_error: BaseException | None = None
            for attempt in (1, 2):
                try:
                    final_state = await graph.ainvoke(
                        initial_state(state["task"], max_revisions=max_revisions)
                    )
                    architecture = final_state.get("architecture") or ""
                    api_spec = final_state.get("api_spec") or ""
                    verdict = await judge.ainvoke(
                        {
                            "requirements": state["task"],
                            "architecture": architecture,
                            "api_spec": api_spec,
                        }
                    )
                    findings = verdict["security_findings"]
                    approved = is_approved(findings)
                    guard = critical_rule_guard(architecture, api_spec)
                    if guard and approved:
                        findings = (
                            "CHANGES REQUESTED\n"
                            f"CriticalRuleGuard: {guard}\n"
                            f"{findings}"
                        )
                        approved = False
                    return {
                        "persona_id": persona.get("id"),
                        "prompt": persona["prompt"],
                        "revisions": final_state.get("revision_count", 0),
                        "approved": approved,
                        "brief_verified": bool(verdict.get("brief_verified")),
                        "weighted": weighted_findings(findings),
                        "findings_length": len(findings or ""),
                        "architecture": architecture,
                        "api_spec": api_spec,
                        "findings": findings,
                        "outcome": "approved" if approved else "not_approved",
                    }
                except Exception as exc:  # retry once, then record and continue
                    last_error = exc
                    if attempt == 1:
                        delay = 0.1 + random.random() * 0.1
                        logger.warning(
                            "Harness candidate %s failed (%s); retrying once",
                            persona.get("id"),
                            exc,
                        )
                        await asyncio.sleep(delay)
            reason = _failure_reason(last_error) if last_error else "provider_error"
            logger.warning(
                "Harness candidate %s failed twice (%s): %s",
                persona.get("id"),
                reason,
                last_error,
            )
            return {
                "persona_id": persona.get("id"),
                "prompt": persona["prompt"],
                "failed": True,
                "outcome": "failed",
                "failure_reason": reason,
            }

        outcomes = await asyncio.gather(
            *(run_candidate(persona, graph) for persona, graph in probes),
            return_exceptions=True,
        )
        results = []
        failures = []
        for (persona, _), outcome in zip(probes, outcomes):
            if isinstance(outcome, BaseException):
                failures.append(
                    {
                        "persona_id": persona.get("id"),
                        "prompt": persona["prompt"],
                        "outcome": "failed",
                        "failure_reason": _failure_reason(outcome),
                    }
                )
            elif outcome.get("failed"):
                failures.append(outcome)
            else:
                results.append(outcome)
        if not results:
            raise AllCandidatesFailed(failures)
        return {"probe_results": results, "failures": failures}

    return probe_node


def compose_node(state: HarnessState) -> dict:
    return {"winning_prompt": pick_winner(state["probe_results"])["prompt"]}


def build_harness_optimizer(
    llm=None, agent_llm_factory=None, judge_llm=None, judge_retriever=None, max_revisions=None
):
    """Compile the propose -> probe -> compose optimizer graph.

    `llm` powers the propose node. `agent_llm_factory` is called once per
    candidate so parallel probes never share an LLM client. `judge_llm` runs
    the baseline security pipeline (default persona) that scores every
    candidate's final design; `judge_retriever` grounds it like production.
    """
    llm = llm if llm is not None else get_llm()
    factory = agent_llm_factory if agent_llm_factory is not None else get_llm
    judge_llm = judge_llm if judge_llm is not None else get_llm(temperature=0.0)
    if judge_retriever is None and settings.SECURITY_RAG_ENABLED:
        judge_retriever = load_default_retriever()
    judge = build_security_subgraph(judge_llm, retriever=judge_retriever)

    builder = StateGraph(HarnessState)
    builder.add_node("propose", make_propose_node(llm))
    builder.add_node("probe", make_probe_node(factory, judge, max_revisions))
    builder.add_node("compose", compose_node)

    builder.add_edge(START, "propose")
    builder.add_edge("propose", "probe")
    builder.add_edge("probe", "compose")
    builder.add_edge("compose", END)

    return builder.compile()


@lru_cache(maxsize=1)
def get_harness_optimizer():
    """Process-wide optimizer app, mirroring get_multi_agent_app.

    It applies HARNESS_REASONING_EFFORT and HARNESS_MAX_REVISIONS like
    scripts/run_harness.py; the route checks HARNESS_MAX_MODEL_CALLS first.
    """
    effort = settings.HARNESS_REASONING_EFFORT or None
    return build_harness_optimizer(
        llm=get_llm(reasoning_effort=effort),
        agent_llm_factory=lambda: get_llm(reasoning_effort=effort),
        judge_llm=get_llm(temperature=0.0, reasoning_effort=effort),
        max_revisions=harness_max_revisions(),
    )


def api_model_calls() -> int:
    """Upper bound on model calls for one /project/optimize request.

    Per candidate attempt: every round is a design plus a 3-call audit, then
    the judge's 3-call audit; a failed candidate is retried once. Plus one
    propose call. (Retries of an empty reply inside a call are not counted.)
    """
    rounds = harness_max_revisions() + 1
    attempt = rounds * (1 + SECURITY_AUDIT_CALLS) + SECURITY_AUDIT_CALLS
    return settings.HARNESS_CANDIDATES * 2 * attempt + 1

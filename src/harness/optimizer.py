import asyncio
import json
import logging
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
from src.core.llm import get_llm
from src.knowledge.retriever import load_default_retriever

logger = logging.getLogger(__name__)

PROPOSE_SYSTEM_PROMPT = (
    "You are an AI prompt optimizer for a system design review board. "
    "Output only a valid JSON array of strings in English."
)

FALLBACK_CANDIDATES = [
    "You are a strict compliance auditor. Review the design against OWASP API "
    "Security Top 10 and PCI-DSS; cite the exact control each finding violates.",
    "You are a red-team adversary. Hunt for business-logic abuse, broken "
    "authorization chains and realistic attack paths an attacker would use.",
    "You are a cloud security architect. Scrutinize IAM boundaries, network "
    "segmentation, multi-tenant isolation, secrets management and blast radius.",
    "You are a privacy and data-protection reviewer. Focus on PII flows, data "
    "minimization, retention, encryption and regulatory exposure.",
    "You are an abuse-prevention engineer. Prioritize rate limiting, idempotency, "
    "replay protection, fraud paths and resource-exhaustion vectors.",
]


class HarnessState(TypedDict):
    task: str
    candidate_prompts: list[str]
    probe_results: list[dict]
    winning_prompt: str


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
        response = llm.invoke(
            [
                SystemMessage(content=PROPOSE_SYSTEM_PROMPT),
                HumanMessage(
                    content=build_propose_prompt(state["task"], settings.HARNESS_CANDIDATES)
                ),
            ]
        )
        return {
            "candidate_prompts": parse_candidates(
                response.content, settings.HARNESS_CANDIDATES
            )
        }

    return propose_node


def make_probe_node(agent_llm_factory, judge):
    """Run every candidate in parallel, then score each final design with `judge`.

    Candidates only add emphasis on top of SECURITY_SYSTEM_PROMPT, and approval
    comes from one fixed baseline auditor rather than the candidate's own
    verdict, so a lenient persona cannot win by approving weak designs.
    """

    async def probe_node(state: HarnessState) -> dict:
        probes = []
        for prompt in state["candidate_prompts"]:
            graph = build_multi_agent(
                llm=agent_llm_factory(),
                security_prompt=with_review_emphasis(prompt),
            )
            probes.append((prompt, graph))

        async def run_candidate(prompt: str, graph) -> dict:
            final_state = await graph.ainvoke(initial_state(state["task"]))
            architecture = final_state.get("architecture") or ""
            api_spec = final_state.get("api_spec") or ""
            verdict = await judge.ainvoke(
                {"architecture": architecture, "api_spec": api_spec}
            )
            findings = verdict["security_findings"]
            return {
                "prompt": prompt,
                "revisions": final_state.get("revision_count", 0),
                "approved": is_approved(findings),
                "architecture": architecture,
                "api_spec": api_spec,
                "findings": findings,
            }

        outcomes = await asyncio.gather(
            *(run_candidate(prompt, graph) for prompt, graph in probes),
            return_exceptions=True,
        )
        results = []
        for (prompt, _), outcome in zip(probes, outcomes):
            if isinstance(outcome, BaseException):
                logger.warning("Harness probe failed for candidate %r: %s", prompt[:80], outcome)
            else:
                results.append(outcome)
        if not results:
            raise RuntimeError("All harness probes failed; see logs for details.")
        return {"probe_results": results}

    return probe_node


def score_result(result: dict) -> tuple:
    """Approval beats rejection, then fewest revisions, then leanest report."""
    return (
        not result.get("approved", False),
        result.get("revisions", 0),
        len(result.get("findings") or ""),
    )


def pick_winner(results: list[dict]) -> dict:
    return min(results, key=score_result)


def compose_node(state: HarnessState) -> dict:
    return {"winning_prompt": pick_winner(state["probe_results"])["prompt"]}


def build_harness_optimizer(
    llm=None, agent_llm_factory=None, judge_llm=None, judge_retriever=None
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
    builder.add_node("probe", make_probe_node(factory, judge))
    builder.add_node("compose", compose_node)

    builder.add_edge(START, "propose")
    builder.add_edge("propose", "probe")
    builder.add_edge("probe", "compose")
    builder.add_edge("compose", END)

    return builder.compile()


@lru_cache(maxsize=1)
def get_harness_optimizer():
    """Process-wide optimizer app, mirroring get_multi_agent_app."""
    return build_harness_optimizer()

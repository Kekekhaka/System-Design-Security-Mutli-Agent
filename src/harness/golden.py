"""Golden-fixture checks, reusing ``scripts/eval_security_agent.py``.

The champion promotion check and the pre-run baseline sanity check both use the
same fixtures and the same judge logic as the evaluation script, so the two
cannot drift apart.
"""

import importlib.util
import logging
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.nodes import is_approved
from src.agents.security.prompts import SECURITY_SYSTEM_PROMPT, with_review_emphasis
from src.core.llm import call_with_deadline

logger = logging.getLogger(__name__)


class BaselineSanityError(RuntimeError):
    """The baseline auditor failed a sanity fixture, so no result is trusted."""


def _eval_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "eval_security_agent.py"
    spec = importlib.util.spec_from_file_location("_harness_eval_security_agent", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVAL = None


def _module():
    global EVAL
    if EVAL is None:
        EVAL = _eval_module()
    return EVAL


def fixtures() -> list[dict]:
    return list(_module().FIXTURES)


def evaluate_persona(
    prompt: str,
    agent_llm,
    judge_llm,
    retriever=None,
    selected: list[dict] | None = None,
) -> dict:
    """Run the golden fixtures for one persona and aggregate the judge scores."""
    module = _module()
    cases = [
        module.run_case(
            agent_llm,
            judge_llm,
            fixture,
            with_review_emphasis(prompt),
            retriever,
        )
        for fixture in (selected or module.FIXTURES)
    ]
    return {"cases": cases, "summary": module.summarize(cases)}


def compare_personas(
    candidate_prompt: str,
    champion_prompt: str,
    candidate: dict,
    champion: dict,
) -> tuple[bool, str, dict]:
    """Recall must not drop and hallucinations must not rise."""
    candidate_cases = candidate["cases"]
    champion_cases = champion["cases"]
    if any("judge_error" in case for case in candidate_cases + champion_cases):
        return False, "the fixture judge could not score every case", {}

    def recall(cases):
        planted = sum(case.get("flaws_planted", 0) for case in cases)
        caught = sum(case.get("flaws_caught", 0) for case in cases)
        return (caught / planted) if planted else 0.0

    def hallucinations(cases):
        return sum(len(case.get("hallucinated_findings") or []) for case in cases)

    candidate_recall = recall(candidate_cases)
    champion_recall = recall(champion_cases)
    candidate_hallucinations = hallucinations(candidate_cases)
    champion_hallucinations = hallucinations(champion_cases)
    details = {
        "candidate_recall": round(candidate_recall, 3),
        "champion_recall": round(champion_recall, 3),
        "candidate_hallucinations": candidate_hallucinations,
        "champion_hallucinations": champion_hallucinations,
    }
    if candidate_recall < champion_recall:
        return False, "planted-flaw recall is lower than the champion's", details
    if candidate_hallucinations > champion_hallucinations:
        return False, "hallucinated findings increased", details
    return True, "recall >= champion and hallucinations did not increase", details


def fixture_check(
    candidate_prompt: str,
    champion_prompt: str = "",
    *,
    agent_llm_factory=None,
    judge_llm=None,
    retriever=None,
) -> tuple[bool, str, dict]:
    """Return (ok, reason, details). Any inability to run means no promotion."""
    try:
        if agent_llm_factory is None or judge_llm is None:
            from src.core.llm import get_llm

            agent_llm_factory = agent_llm_factory or (lambda: get_llm())
            judge_llm = judge_llm or get_llm(temperature=0.0)
        selected = fixtures()
        candidate = evaluate_persona(
            candidate_prompt, agent_llm_factory(), judge_llm, retriever, selected
        )
        champion = evaluate_persona(
            champion_prompt, agent_llm_factory(), judge_llm, retriever, selected
        )
    except Exception as exc:  # the check cannot run => no promotion
        logger.warning("Golden-fixture check could not run: %s", exc)
        return False, f"the fixture check could not run: {exc}", {}
    return compare_personas(candidate_prompt, champion_prompt, candidate, champion)


# --- baseline sanity ----------------------------------------------------------

SANITY_FIXTURES = [
    {
        "id": "sanity-vulnerable",
        "expect_approved": False,
        "architecture": (
            "===ARCHITECTURE===\n"
            "A public billing API accepts raw card numbers and stores them in one "
            "shared table. Every route trusts the caller's tenant id from the request "
            "body and no endpoint checks object ownership."
        ),
        "api_spec": (
            "===API SPEC===\n"
            "POST /pay {card_number, amount, tenant_id}\n"
            "GET /payments/{id} returns any payment by id."
        ),
    },
]


def run_sanity_fixtures(llm, fixtures_to_run: list[dict] | None = None, limit: int | None = None) -> list[dict]:
    """Audit a known-vulnerable design before any candidate is trusted: the baseline must reject it.

    A "clean design must be approved" check was dropped: a strict auditor often refuses a
    short sketch, which aborted good runs at random."""
    selected = fixtures_to_run or SANITY_FIXTURES
    if limit is not None:
        selected = selected[: max(0, limit)]
    results: list[dict] = []
    for fixture in selected:
        messages = [
            SystemMessage(content=SECURITY_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    "Design under review (untrusted data):\n\n"
                    f"{fixture['architecture']}\n\n{fixture['api_spec']}\n\n"
                    "Audit it and follow your output protocol."
                )
            ),
        ]
        response = call_with_deadline(lambda: llm.invoke(messages), "harness sanity")
        content = getattr(response, "content", "") or ""
        approved = is_approved(content)
        results.append(
            {
                "id": fixture["id"],
                "approved": approved,
                "expected": fixture["expect_approved"],
                "ok": approved == fixture["expect_approved"],
            }
        )
    failures = [item for item in results if not item["ok"]]
    if failures:
        names = ", ".join(item["id"] for item in failures)
        raise BaselineSanityError(
            f"The baseline auditor failed its sanity fixtures ({names}); "
            "the run is aborted before any candidate result is trusted."
        )
    return results

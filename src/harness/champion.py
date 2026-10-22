"""Champion promotion and rollback.

A challenger may only become champion when it has beaten the champion in at
least three runs on at least two different tasks AND a golden-fixture check
shows no loss of recall and no increase in hallucinated findings. Promotion is
always a manual operator action.
"""

import logging

from src.harness.personas import validate_persona
from src.harness.seeds import utc_now
from src.harness.store import prompt_sha256

logger = logging.getLogger(__name__)

MIN_WINS = 3
MIN_TASKS = 2
MAX_CHAMPION_HISTORY = 50


class PromotionError(RuntimeError):
    """The promotion requirements are not met."""


def qualifying_record(history: list[dict], candidate_id: str, champion_id: str | None) -> dict:
    """Count runs where the candidate beat the champion on a distinct task."""
    wins = 0
    tasks: list[str] = []
    for entry in history:
        if entry.get("type", "run") != "run":
            continue
        candidates = {
            item.get("persona_id"): item for item in entry.get("candidates") or []
        }
        candidate = candidates.get(candidate_id)
        if candidate is None or candidate.get("outcome") == "failed":
            continue
        if champion_id is None:
            beaten = entry.get("winner") == candidate_id
        else:
            champion = candidates.get(champion_id)
            champion_failed = champion is None or champion.get("outcome") == "failed"
            candidate_score = candidate.get("weighted")
            champion_score = None if champion is None else champion.get("weighted")
            lower = (
                candidate_score is not None
                and champion_score is not None
                and candidate_score < champion_score
            )
            beaten = champion_failed or lower
        if beaten:
            wins += 1
            task = entry.get("task_hash")
            if task and task not in tasks:
                tasks.append(task)
    return {"wins": wins, "tasks": len(tasks), "task_hashes": tasks}


def check_requirements(
    store: dict, persona_id: str, history: list[dict]
) -> dict:
    champion = store.get("champion") or {}
    champion_id = champion.get("persona_id")
    record = qualifying_record(history, persona_id, champion_id)
    if record["wins"] < MIN_WINS:
        raise PromotionError(
            f"{persona_id} has beaten the champion in {record['wins']} run(s); "
            f"{MIN_WINS} are required"
        )
    if record["tasks"] < MIN_TASKS:
        raise PromotionError(
            f"{persona_id} has beaten the champion on {record['tasks']} task(s); "
            f"{MIN_TASKS} are required"
        )
    return record


def promote(
    store: dict,
    persona_id: str,
    reason: str = "",
    history: list[dict] | None = None,
    *,
    fixture_checker=None,
    agent_llm_factory=None,
    judge_llm=None,
    retriever=None,
) -> dict:
    """Promote a persona. Mutates `store`; the caller saves it."""
    persona = next(
        (item for item in store.get("personas", []) if item.get("id") == persona_id), None
    )
    if persona is None:
        raise PromotionError(f"Persona {persona_id} does not exist")
    valid, invalid_reason = validate_persona(persona)
    if not valid:
        raise PromotionError(f"Persona {persona_id} is invalid: {invalid_reason}")
    if persona.get("status") != "active":
        raise PromotionError(f"Persona {persona_id} is not active")

    champion = store.get("champion") or {}
    current_id = champion.get("persona_id")
    if current_id == persona_id:
        return {
            "ok": True,
            "champion": persona_id,
            "previous_champion": champion.get("previous_persona_id"),
            "prompt_sha256": prompt_sha256(persona["prompt"]),
            "checked_at": utc_now(),
            "noop": True,
        }

    history = history or []
    record = check_requirements(store, persona_id, history)

    current_persona = next(
        (item for item in store.get("personas", []) if item.get("id") == current_id), None
    )
    champion_prompt = current_persona.get("prompt", "") if current_persona else ""
    if fixture_checker is None:
        from src.harness.golden import fixture_check

        ok, fixture_reason, details = fixture_check(
            persona["prompt"],
            champion_prompt,
            agent_llm_factory=agent_llm_factory,
            judge_llm=judge_llm,
            retriever=retriever,
        )
    else:
        try:
            result = fixture_checker(persona["prompt"], champion_prompt)
        except Exception as exc:
            raise PromotionError(
                f"The golden-fixture check could not run: {exc}"
            ) from exc
        if isinstance(result, tuple):
            ok, fixture_reason = result[0], result[1]
            details = result[2] if len(result) > 2 else {}
        else:
            ok, fixture_reason, details = bool(result), "fixture check passed", {}
    if not ok:
        raise PromotionError(f"The golden-fixture check failed: {fixture_reason}")

    now = utc_now()
    full_reason = (
        f"{record['wins']} wins on {record['tasks']} tasks; {fixture_reason}"
    )
    if reason:
        full_reason = f"{full_reason}; operator note: {reason}"
    store["champion"] = {
        "persona_id": persona_id,
        "since": now,
        "previous_persona_id": current_id,
        "reason": full_reason,
        "prompt_sha256": prompt_sha256(persona["prompt"]),
    }
    history_event = {
        "action": "promote",
        "persona_id": persona_id,
        "previous_persona_id": current_id,
        "reason": full_reason,
        "at": now,
        "details": details,
    }
    store.setdefault("champion_history", []).append(history_event)
    store["champion_history"] = store["champion_history"][-MAX_CHAMPION_HISTORY:]
    logger.info("Promoted harness champion %s (%s)", persona_id, full_reason)
    return {
        "ok": True,
        "champion": persona_id,
        "previous_champion": current_id,
        "prompt_sha256": store["champion"]["prompt_sha256"],
        "checked_at": now,
        "reason": full_reason,
    }


def rollback(store: dict, reason: str = "") -> dict:
    """Restore the previous champion; with none, the champion becomes null."""
    champion = store.get("champion")
    if not champion:
        return {"ok": True, "champion": None, "rolled_back_from": None}

    rolled_back_from = champion.get("persona_id")
    previous_id = champion.get("previous_persona_id")
    previous = next(
        (item for item in store.get("personas", []) if item.get("id") == previous_id), None
    )
    restored = previous_id if previous and previous.get("status") == "active" else None
    now = utc_now()
    if restored:
        store["champion"] = {
            "persona_id": restored,
            "since": now,
            "previous_persona_id": None,
            "reason": f"Rollback from {rolled_back_from}" + (f": {reason}" if reason else ""),
            "prompt_sha256": prompt_sha256(previous["prompt"]),
        }
    else:
        store["champion"] = None
    event = {
        "action": "rollback",
        "persona_id": restored,
        "rolled_back_from": rolled_back_from,
        "reason": reason or "operator rollback",
        "at": now,
    }
    store.setdefault("champion_history", []).append(event)
    store["champion_history"] = store["champion_history"][-MAX_CHAMPION_HISTORY:]
    logger.info("Rolled back harness champion %s to %s", rolled_back_from, restored)
    return {"ok": True, "champion": restored, "rolled_back_from": rolled_back_from}

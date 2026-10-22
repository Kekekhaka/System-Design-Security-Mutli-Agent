"""Run one Harness2 optimization and let the persona library learn from it.

    python scripts/run_harness.py --brief "Design a booking API for a guest house."
    python scripts/run_harness.py --brief-file brief.md
    python scripts/run_harness.py --brief-file body.json   # JSON with "requirements"

The run selects the best persona from the library (exploit), asks the model for
one challenger improved from that persona's recorded weaknesses, fills the rest
from the seeds, runs the full designer <-> security loop for every candidate in
parallel, re-audits every final design with one fixed baseline auditor, and picks
the candidate approved first, then the lowest severity-weighted findings.

Cost, with C = candidates, R = revisions per candidate:

    candidate_calls = C * (2 * (R + 1) + 1)
    challenger_call = 1 when the library has an active persona (always, after seeding)
    sanity_calls    = HARNESS_SANITY_FIXTURES (default 1: one vulnerable design the baseline must reject)
    total           = candidate_calls + challenger_call + sanity_calls

`--dry-run` prints that total and exits without making a model call or touching
the store. Reasoning is off by default (`HARNESS_REASONING_EFFORT=none`) so a
candidate cannot spend its whole output budget on hidden reasoning.

Exit codes: 0 success, 2 validation, 3 every candidate failed, 4 integrity/lock,
5 promoted-run requirements not met (not used here; see harness_champion.py).
"""

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import settings
from src.core.llm import ModelCallTimeout, ModelOutputError, get_llm
from src.harness.golden import BaselineSanityError, run_sanity_fixtures
from src.harness.lock import ExclusiveLock, LockError
from src.harness.optimizer import (
    AllCandidatesFailed,
    build_harness_optimizer,
    pick_winner,
)
from src.harness.personas import record_outcome, retire_worst, select_candidates
from src.harness.scoring import severity_counts
from src.harness.seeds import utc_now
from src.harness.store import (
    DuplicateRunId,
    StoreError,
    append_history,
    default_history_path,
    default_store_path,
    load_store,
    read_history,
    resolve_hmac_key,
    save_store,
    task_hash,
)

logger = logging.getLogger(__name__)


def estimate_model_calls(
    candidates: int,
    max_revisions: int,
    challenger: int = 1,
    sanity: int = 2,
) -> int:
    return candidates * (2 * (max_revisions + 1) + 1) + challenger + sanity


def read_task(args) -> str:
    path = args.brief_file or args.task_file
    if path:
        text = Path(path).read_text(encoding="utf-8-sig").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        return str(data.get("requirements", "")).strip() if isinstance(data, dict) else text
    return (args.brief or args.task or "").strip()


def _library_has_persona(path: Path) -> bool:
    """Read-only peek for --dry-run: a missing or corrupt store starts from seeds."""
    if not path.exists():
        return True
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    personas = document.get("personas") if isinstance(document, dict) else None
    return bool(personas)


def _resolve_counts(args) -> tuple[int, int]:
    candidates = args.candidates or settings.HARNESS_CANDIDATES
    candidates = max(1, min(candidates, max(1, settings.HARNESS_CANDIDATES)))
    max_revisions = args.max_revisions
    if max_revisions is None:
        max_revisions = settings.HARNESS_MAX_REVISIONS or settings.MAX_REVISIONS
    max_revisions = max(0, min(max_revisions, max(0, settings.MAX_REVISIONS)))
    return candidates, max_revisions


def build_history_entry(
    *,
    run_id: str,
    task: str,
    reasoning: str,
    candidates: list[dict],
    failures: list[dict],
    winner: str | None,
    champion_id: str | None,
    candidate_count: int,
    max_revisions: int,
    key: str | None,
    now: str | None = None,
) -> dict:
    lowered = {item.get("persona_id") or item.get("prompt"): item for item in failures}
    records = []
    for result in candidates:
        records.append(
            {
                "persona_id": result.get("persona_id"),
                "outcome": result.get("outcome", "not_approved"),
                "failure_reason": None,
                "weighted": result.get("weighted"),
                "revisions": result.get("revisions"),
                "findings_length": result.get("findings_length"),
            }
        )
    for failure in lowered.values():
        records.append(
            {
                "persona_id": failure.get("persona_id"),
                "outcome": "failed",
                "failure_reason": failure.get("failure_reason"),
                "weighted": None,
                "revisions": None,
                "findings_length": None,
            }
        )
    return {
        "type": "run",
        "run_id": run_id,
        "timestamp": now or utc_now(),
        "task_hash": task_hash(task, key),
        "settings": {
            "model": settings.OPENROUTER_MODEL,
            "reasoning_effort": reasoning,
            "reasoning_tag": reasoning,
            "max_revisions": max_revisions,
            "candidate_count": candidate_count,
            "champion_id": champion_id,
        },
        "candidates": records,
        "winner": winner,
    }


def _write_artifacts(out_dir: Path, summary: dict, task: str, results: list[dict], winner_prompt: str | None, seconds: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "seconds": round(seconds),
        "task": task,
        "settings": summary["settings"],
        "candidates": summary["candidates"],
        "winner": summary["winner"],
        "winning_prompt": winner_prompt,
    }
    (out_dir / "result.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8", newline="\n"
    )
    if winner_prompt:
        (out_dir / "winner-persona.txt").write_text(
            winner_prompt.rstrip() + "\n", encoding="utf-8", newline="\n"
        )
    for index, probe in enumerate(results, start=1):
        body = (
            f"# Candidate {index}\n\nPersona id: {probe.get('persona_id')}\n\n"
            f"Persona:\n{probe.get('prompt')}\n\n"
            f"Approved by the baseline auditor: {probe.get('approved')}  |  "
            f"revisions: {probe.get('revisions')}  |  weighted: {probe.get('weighted')}\n\n"
            f"## Architecture\n{probe.get('architecture')}\n\n"
            f"## API spec\n{probe.get('api_spec')}\n\n"
            f"## Baseline audit of the final design\n{probe.get('findings')}\n"
        )
        (out_dir / f"candidate-{index}.md").write_text(body, encoding="utf-8", newline="\n")


def summarise(results: list[dict], failures: list[dict], winner: dict | None) -> dict:
    return {
        "winner": winner.get("persona_id") if winner else None,
        "candidates": [
            {
                "persona_id": item.get("persona_id"),
                "outcome": item.get("outcome"),
                "weighted": item.get("weighted"),
                "revisions": item.get("revisions"),
                "findings_length": item.get("findings_length"),
                "approved": item.get("approved"),
                "findings_by_severity": severity_counts(item.get("findings")),
            }
            for item in results
        ]
        + [
            {
                "persona_id": item.get("persona_id"),
                "outcome": "failed",
                "failure_reason": item.get("failure_reason"),
            }
            for item in failures
        ],
    }


async def run_graph(task: str, candidates: list[dict]) -> dict:
    graph = build_harness_optimizer()
    collected: dict = {}
    async for update in graph.astream(
        {
            "task": task,
            "candidate_prompts": [persona["prompt"] for persona in candidates],
            "candidate_personas": candidates,
        },
        stream_mode="updates",
    ):
        for node, delta in update.items():
            collected.update(delta or {})
    return collected


def _persist(
    *,
    store: dict,
    chosen: list[dict],
    results: list[dict],
    failures: list[dict],
    winner: dict | None,
    history: dict,
    store_path: Path,
    history_path: Path,
    key: str | None,
) -> None:
    known = {persona.get("id") for persona in store.get("personas", [])}
    for persona in chosen:
        if persona.get("id") not in known:
            store.setdefault("personas", []).append(persona)

    winner_id = winner.get("persona_id") if winner else None
    by_id = {item.get("persona_id"): item for item in results}
    failed_ids = {item.get("persona_id") for item in failures}
    for persona in chosen:
        persona_id = persona.get("id")
        if persona_id in failed_ids:
            record_outcome(persona, outcome="failed")
            continue
        result = by_id.get(persona_id)
        if result is None:
            record_outcome(persona, outcome="failed")
            continue
        record_outcome(
            persona,
            outcome=result.get("outcome", "not_approved"),
            weighted=result.get("weighted"),
            revisions=result.get("revisions"),
            won=persona_id == winner_id,
            approved=bool(result.get("approved")),
            findings=result.get("findings"),
        )

    retire_worst(store, settings.HARNESS_LIBRARY_CAP)
    append_history(history, history_path, key=key)
    save_store(store, store_path, key=key)


def fail(code: int, error_code: str, run_id: str | None, message: str) -> int:
    """Report a failed run as one JSON line on stderr (machine-readable) and return its exit code."""
    print(json.dumps({"error_code": error_code, "run_id": run_id, "message": message}), file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Harness2 optimizer once and update the persona library."
    )
    parser.add_argument("task", nargs="?", help="The design requirements as text")
    parser.add_argument("--brief", help="The task brief text")
    parser.add_argument("--brief-file", help="Read the brief from this file")
    parser.add_argument("--task-file", help="Alias of --brief-file (JSON or text)")
    parser.add_argument("--candidates", type=int, help="Candidate count (capped by config)")
    parser.add_argument("--max-revisions", type=int, help="Per-candidate revision cap")
    parser.add_argument(
        "--reasoning", choices=["none", "low", "medium", "high"], help="Reasoning effort"
    )
    parser.add_argument("--run-id", help="Idempotency key; duplicates are rejected")
    parser.add_argument("--dry-run", action="store_true", help="Print the model-call count and exit")
    parser.add_argument("--json", action="store_true", help="Emit a JSON summary")
    parser.add_argument("--out", help="Output folder (default: runs/harness/<timestamp>)")
    args = parser.parse_args(argv)

    if args.brief and (args.brief_file or args.task_file):
        parser.error("use either --brief or --brief-file, not both")
    task = read_task(args)
    if not task:
        parser.error("give --brief, --brief-file, or the requirements as text")

    candidates, max_revisions = _resolve_counts(args)
    reasoning = args.reasoning or settings.HARNESS_REASONING_EFFORT or "none"
    store_path = default_store_path()
    history_path = default_history_path(store_path)
    key = resolve_hmac_key()

    challenger = 1 if _library_has_persona(store_path) else 0
    sanity = max(0, min(settings.HARNESS_SANITY_FIXTURES, 1))
    calls = estimate_model_calls(candidates, max_revisions, challenger, sanity)

    if args.dry_run:
        payload = {
            "dry_run": True,
            "model_calls": calls,
            "settings": {
                "candidates": candidates,
                "max_revisions": max_revisions,
                "reasoning_effort": reasoning,
                "challenger": challenger,
                "sanity": sanity,
            },
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"model_calls: {calls}")
            print(
                f"  candidates={candidates} max_revisions={max_revisions} "
                f"challenger={challenger} sanity={sanity}"
            )
        return 0

    settings.LLM_REASONING_EFFORT = reasoning
    # The supervisor reads MAX_REVISIONS, so a smaller per-run cap must apply there too.
    settings.MAX_REVISIONS = max_revisions
    if settings.HARNESS_MAX_MODEL_CALLS and calls > settings.HARNESS_MAX_MODEL_CALLS:
        return fail(
            4, "over_budget", args.run_id,
            f"The run needs {calls} model calls, above HARNESS_MAX_MODEL_CALLS="
            f"{settings.HARNESS_MAX_MODEL_CALLS}.",
        )
    run_id = args.run_id or str(uuid.uuid4())
    existing = read_history(history_path, key=key)
    if any(entry.get("run_id") == run_id for entry in existing):
        return fail(2, "duplicate_run_id", run_id, f"Run {run_id} is already in the history; not re-executed.")

    out_dir = (
        Path(args.out)
        if args.out
        else Path("runs") / "harness" / datetime.now().strftime("%Y%m%d-%H%M%S")
    ).resolve()

    started = time.monotonic()
    try:
        with ExclusiveLock(store_path.parent / ".harness.lock"):
            store = load_store(store_path, key=key)
            champion = (store.get("champion") or {}).get("persona_id")
            llm = get_llm() if settings.OPENROUTER_API_KEY else None
            if llm is not None and sanity:
                run_sanity_fixtures(llm, limit=sanity)
            chosen = select_candidates(store, llm=llm, count=candidates, task=task)
            try:
                collected = asyncio.run(run_graph(task, chosen))
            except AllCandidatesFailed as exc:
                failures = exc.failures
                history = build_history_entry(
                    run_id=run_id,
                    task=task,
                    reasoning=reasoning,
                    candidates=[],
                    failures=failures,
                    winner=None,
                    champion_id=champion,
                    candidate_count=candidates,
                    max_revisions=max_revisions,
                    key=key,
                )
                _persist(
                    store=store,
                    chosen=chosen,
                    results=[],
                    failures=failures,
                    winner=None,
                    history=history,
                    store_path=store_path,
                    history_path=history_path,
                    key=key,
                )
                summary = summarise([], failures, None)
                summary["run_id"] = run_id
                if args.json:
                    print(json.dumps(summary, indent=2))
                else:
                    print(
                        "Every candidate failed; the run is recorded in the history.",
                        file=sys.stderr,
                    )
                return 3

            results = collected.get("probe_results") or []
            failures = collected.get("failures") or []
            winner = pick_winner(results) if results else None
            history = build_history_entry(
                run_id=run_id,
                task=task,
                reasoning=reasoning,
                candidates=results,
                failures=failures,
                winner=winner.get("persona_id") if winner else None,
                champion_id=champion,
                candidate_count=candidates,
                max_revisions=max_revisions,
                key=key,
            )
            _persist(
                store=store,
                chosen=chosen,
                results=results,
                failures=failures,
                winner=winner,
                history=history,
                store_path=store_path,
                history_path=history_path,
                key=key,
            )
    except DuplicateRunId:
        return fail(2, "duplicate_run_id", run_id, f"Run {run_id} is already in the history; not re-executed.")
    except LockError as exc:
        return fail(4, "not_started", run_id, f"Harness run not started: {exc}")
    except (BaselineSanityError, StoreError) as exc:
        return fail(4, "aborted", run_id, f"Harness run aborted: {exc}")
    except (ModelOutputError, ModelCallTimeout, RuntimeError) as exc:
        return fail(4, "run_failed", run_id, f"Harness run failed: {exc}")

    summary = summarise(results, failures, winner)
    summary["settings"] = {
        "candidates": candidates,
        "max_revisions": max_revisions,
        "reasoning_effort": reasoning,
        "model": settings.OPENROUTER_MODEL,
    }
    summary["run_id"] = run_id
    _write_artifacts(
        out_dir,
        summary,
        task,
        results,
        winner.get("prompt") if winner else None,
        time.monotonic() - started,
    )

    if args.json:
        print(json.dumps({"run_id": run_id, **summary}, indent=2))
    else:
        print("\nCandidates:")
        for item in summary["candidates"]:
            if item["outcome"] == "failed":
                print(f"  FAILED {item['persona_id']} ({item.get('failure_reason')})")
                continue
            counts = item["findings_by_severity"]
            print(
                f"  {'WINNER ' if item['persona_id'] == summary['winner'] else '       '}"
                f"approved={item['approved']!s:5} weighted={item['weighted']:3} "
                f"revisions={item['revisions']} findings C/H/M/L="
                f"{counts['Critical']}/{counts['High']}/{counts['Medium']}/{counts['Low']}  "
                f"{item['persona_id']}"
            )
        print(f"\nWinner: {summary['winner']}")
        print(f"Saved to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Run the Harness2 optimizer once, without the API, and save everything it learned.

    python scripts/run_harness.py "Design a booking API for a guest house."
    python scripts/run_harness.py --task-file body.json        # JSON with "requirements", or plain text

The optimizer proposes HARNESS_CANDIDATES security-reviewer personas, runs the full
designer <-> security loop for each one in parallel, re-audits every final design with one
fixed baseline auditor, and picks the persona that was approved in the fewest revisions.

Cost: 1 + N x (4R + 3) model calls, where N = HARNESS_CANDIDATES and R = the revisions a
candidate needs (at most MAX_REVISIONS). For a cheap end-to-end check use:
    HARNESS_CANDIDATES=2 MAX_REVISIONS=2 python scripts/run_harness.py ...   (at most 23 calls)

Writes runs/harness/<timestamp>/: result.json (every candidate's persona, revisions,
approval and findings), candidate-N.md (its final design and audit) and winner-persona.txt.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import settings
from src.core.llm import ModelCallTimeout, ModelOutputError
from src.harness.optimizer import build_harness_optimizer, pick_winner

FINDING = re.compile(r"^\d+\.\s+\[(Critical|High|Medium|Low)\]", re.MULTILINE)


def read_task(args) -> str:
    if args.task_file:
        text = Path(args.task_file).read_text(encoding="utf-8-sig").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        return str(data.get("requirements", "")).strip() if isinstance(data, dict) else text
    return (args.task or "").strip()


def severity_counts(findings: str) -> dict[str, int]:
    counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
    for severity in FINDING.findall(findings or ""):
        counts[severity] += 1
    return counts


def summarise(result: dict) -> dict:
    winner = pick_winner(result["probe_results"])
    return {
        "winning_prompt": result["winning_prompt"],
        "candidates": [
            {
                "persona": probe["prompt"],
                "approved": probe["approved"],
                "revisions": probe["revisions"],
                "findings_by_severity": severity_counts(probe["findings"]),
                "findings_chars": len(probe["findings"] or ""),
                "won": probe["prompt"] == winner["prompt"],
            }
            for probe in result["probe_results"]
        ],
    }


def write_run(task: str, result: dict, out_dir: Path, seconds: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "seconds": round(seconds),
        "settings": {
            "candidates": settings.HARNESS_CANDIDATES,
            "max_revisions": settings.MAX_REVISIONS,
            "model": settings.OPENROUTER_MODEL,
        },
        "task": task,
        "proposed_personas": result.get("candidate_prompts", []),
        **summarise(result),
    }
    (out_dir / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8", newline="\n")
    (out_dir / "winner-persona.txt").write_text(result["winning_prompt"].rstrip() + "\n", encoding="utf-8", newline="\n")
    for index, probe in enumerate(result["probe_results"], start=1):
        body = (
            f"# Candidate {index}\n\nPersona:\n{probe['prompt']}\n\n"
            f"Approved by the baseline auditor: {probe['approved']}  |  revisions: {probe['revisions']}\n\n"
            f"## Architecture\n{probe['architecture']}\n\n## API spec\n{probe['api_spec']}\n\n"
            f"## Baseline audit of the final design\n{probe['findings']}\n"
        )
        (out_dir / f"candidate-{index}.md").write_text(body, encoding="utf-8", newline="\n")


async def run(task: str) -> dict:
    graph = build_harness_optimizer()
    started = time.monotonic()
    result: dict = {}
    async for update in graph.astream({"task": task}, stream_mode="updates"):
        for node, delta in update.items():
            result.update(delta or {})
            print(f"[{time.monotonic() - started:6.0f}s] finished {node}", flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Harness2 optimizer once and save the results.")
    parser.add_argument("task", nargs="?", help="The design requirements as text")
    parser.add_argument("--task-file", help="Read the requirements from this file")
    parser.add_argument("--out", help="Output folder (default: runs/harness/<timestamp>)")
    args = parser.parse_args(argv)
    task = read_task(args)
    if not task:
        parser.error("give the requirements as text, or --task-file")

    out_dir = (Path(args.out) if args.out else Path("runs") / "harness" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    print(f"{settings.HARNESS_CANDIDATES} candidates, up to {settings.MAX_REVISIONS} revisions each, model {settings.OPENROUTER_MODEL}", flush=True)
    started = time.monotonic()
    try:
        result = asyncio.run(run(task))
    except (ModelOutputError, ModelCallTimeout, RuntimeError) as exc:
        print(f"Harness run failed: {exc}", file=sys.stderr)
        return 1
    write_run(task, result, out_dir, time.monotonic() - started)

    print("\nCandidates:")
    for item in summarise(result)["candidates"]:
        counts = item["findings_by_severity"]
        print(f"  {'WINNER ' if item['won'] else '       '}approved={item['approved']!s:5} revisions={item['revisions']} "
              f"findings C/H/M/L={counts['Critical']}/{counts['High']}/{counts['Medium']}/{counts['Low']}  {item['persona'][:90]!r}")
    print(f"\nSaved to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

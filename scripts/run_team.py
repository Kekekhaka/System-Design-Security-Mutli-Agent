"""Run the team pipeline on a brief and write every artifact to a run folder.

Usage:
    python scripts/run_team.py "Design a booking API for a small clinic."
    python scripts/run_team.py --brief-file brief.md
    python scripts/run_team.py --brief-file brief.md --answers "Single clinic; no payments."

The team lead sizes and staffs the job; the business analyst (and ideologist,
when staffed) writes the requirements; the architect designs; the security
engineer audits, with fix rounds capped by mode; the team lead signs off.
The brief is sent to the configured model provider (OpenRouter).

Writes runs/<timestamp>/: IDEA_BRIEF.md, REQUIREMENTS_PACK.md, architecture.md,
api-spec.md, security-report.md, SIGN_OFF.md (each when produced) and
team-log.json (plan, handoffs, decisions, status, model calls).

Exit codes: 0 approved, 1 could not run, 2 escalated (security still requests
changes), 3 the team lead needs answers (rerun with --answers).
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.team.graph import build_team_graph, initial_team_state
from src.core.llm import ModelCallTimeout, ModelOutputError

ARTIFACTS = {
    "idea_brief": "IDEA_BRIEF.md",
    "requirements_pack": "REQUIREMENTS_PACK.md",
    "architecture": "architecture.md",
    "api_spec": "api-spec.md",
    "security_findings": "security-report.md",
    "sign_off": "SIGN_OFF.md",
}
EXIT_CODES = {"approved": 0, "escalated": 2, "needs_operator": 3}


def write_run(result: dict, out_dir: Path) -> list[Path]:
    """Write each produced artifact plus team-log.json; return the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for key, filename in ARTIFACTS.items():
        text = result.get(key)
        if text:
            path = out_dir / filename
            path.write_text(text.rstrip() + "\n", encoding="utf-8", newline="\n")
            written.append(path)
    log = {
        "status": result.get("status"),
        "plan": result.get("plan"),
        "revision_count": result.get("revision_count", 0),
        "model_calls": result.get("model_calls", 0),
        "handoffs": result.get("handoffs", []),
        "decisions": result.get("decisions", []),
    }
    path = out_dir / "team-log.json"
    path.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8", newline="\n")
    written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the team pipeline on a brief.")
    parser.add_argument("brief", nargs="?", help="The brief as text")
    parser.add_argument("--brief-file", help="Read the brief from this file")
    parser.add_argument("--answers", help="Answers to the team lead's questions")
    parser.add_argument("--out", help="Output folder (default: runs/<timestamp>)")
    args = parser.parse_args(argv)

    if args.brief_file:
        brief = Path(args.brief_file).read_text(encoding="utf-8-sig").strip()
    else:
        brief = (args.brief or "").strip()
    if not brief:
        parser.error("give a brief, or --brief-file")

    out_dir = Path(args.out) if args.out else Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S")
    graph = build_team_graph()
    try:
        result = graph.invoke(initial_team_state(brief, operator_answers=args.answers))
    except (ModelOutputError, ModelCallTimeout) as exc:
        print(f"Run failed: {exc}", file=sys.stderr)
        return 1

    write_run(result, out_dir)
    status = result.get("status", "")
    print(f"Status: {status}  (model calls: {result.get('model_calls', 0)})")
    for decision in result.get("decisions", []):
        print(f"  - {decision}")
    if status == "needs_operator":
        print("\nThe team lead needs answers before continuing:")
        for question in (result.get("plan") or {}).get("questions", []):
            print(f"  ? {question}")
        print('Rerun with --answers "..."')
    print(f"\nArtifacts: {out_dir}")
    return EXIT_CODES.get(status, 1)


if __name__ == "__main__":
    sys.exit(main())

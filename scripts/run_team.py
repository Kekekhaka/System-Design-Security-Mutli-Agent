"""Run the team pipeline on a brief and write every artifact to a run folder.

Usage:
    python scripts/run_team.py "Design a booking API for a small clinic."
    python scripts/run_team.py --brief-file brief.md --answers "Single clinic; no payments."
    python scripts/run_team.py --brief-file brief.md --build --repo C:/path/to/repo

Design (every run): the team lead sizes and staffs the job; the business analyst
writes the requirements when staffed; the designer designs; the security
engineer audits, looping up to MAX_REVISIONS; the team lead signs off.

Build (--build, only after the design is APPROVED): the engineers and QA are
opencode runs (OPENCODE_MODEL) in a git worktree of --repo on a new branch
team/<run id>; the tests run there for real (TEAM_TEST_COMMAND); the reviewer
and the security engineer review the real diff; failures go back to the
engineers, up to MAX_REVISIONS. Nothing is committed, pushed or merged: you
review the branch and decide.

The brief and the design are sent to the configured model provider (OpenRouter),
and in a build run the briefs also go to opencode's model.

Writes runs/<timestamp>/: REQUIREMENTS_PACK.md, architecture.md, api-spec.md,
security-report.md, and in a build run TEST_REPORT.md, REVIEW_REPORT.md,
code-security-report.md, changes.diff, DEPLOY_PLAN.md, PROJECT_DOCS.md and the
engineers' briefs/ (each when produced); SIGN_OFF.md and team-log.json.

Exit codes: 0 approved, 1 could not run, 2 escalated (CHANGES REQUESTED),
3 the team lead needs answers (rerun with --answers).
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.team.graph import (
    RUN_CONFIG,
    build_team_graph,
    format_test_result,
    initial_team_state,
)
from src.core.llm import ModelCallTimeout, ModelOutputError

ARTIFACTS = {
    "requirements_pack": "REQUIREMENTS_PACK.md",
    "architecture": "architecture.md",
    "api_spec": "api-spec.md",
    "security_findings": "security-report.md",
    "review_findings": "REVIEW_REPORT.md",
    "code_security_findings": "code-security-report.md",
    "diff": "changes.diff",
    "deploy_plan": "DEPLOY_PLAN.md",
    "project_docs": "PROJECT_DOCS.md",
    "sign_off": "SIGN_OFF.md",
}
EXIT_CODES = {"approved": 0, "escalated": 2, "needs_operator": 3}


def _write(path: Path, text: str) -> Path:
    path.write_text(text.rstrip() + "\n", encoding="utf-8", newline="\n")
    return path


def write_run(result: dict, out_dir: Path) -> list[Path]:
    """Write each produced artifact plus team-log.json; return the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for key, filename in ARTIFACTS.items():
        text = result.get(key)
        if text:
            written.append(_write(out_dir / filename, text))
    if result.get("test_result"):
        written.append(_write(out_dir / "TEST_REPORT.md", f"# Test run\n\n{format_test_result(result['test_result'])}"))
    log = {
        "status": result.get("status"),
        "plan": result.get("plan"),
        "revision_count": result.get("revision_count", 0),
        "model_calls": result.get("model_calls", 0),
        "opencode_runs": result.get("opencode_runs", 0),
        "build": {
            "requested": bool(result.get("build")),
            "status": result.get("build_status"),
            "rounds": result.get("build_round"),
            "branch": result.get("branch"),
            "worktree": result.get("worktree"),
            "changed_files": result.get("changed_files", []),
            "qa_violations": result.get("qa_violations", []),
        },
        "handoffs": result.get("handoffs", []),
        "decisions": result.get("decisions", []),
    }
    written.append(_write(out_dir / "team-log.json", json.dumps(log, indent=2)))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the team pipeline on a brief.")
    parser.add_argument("brief", nargs="?", help="The brief as text")
    parser.add_argument("--brief-file", help="Read the brief from this file")
    parser.add_argument("--answers", help="Answers to the team lead's questions")
    parser.add_argument("--out", help="Output folder (default: runs/<timestamp>)")
    parser.add_argument("--build", action="store_true", help="Build the approved design with opencode")
    parser.add_argument("--repo", help="The git repository to build in (required with --build)")
    args = parser.parse_args(argv)

    if args.brief_file:
        brief = Path(args.brief_file).read_text(encoding="utf-8-sig").strip()
    else:
        brief = (args.brief or "").strip()
    if not brief:
        parser.error("give a brief, or --brief-file")
    if args.build and not args.repo:
        parser.error("--build needs --repo, the git repository to build in")

    out_dir = (Path(args.out) if args.out else Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    graph = build_team_graph()
    state = initial_team_state(
        brief, operator_answers=args.answers, build=args.build, repo=args.repo, run_dir=str(out_dir)
    )
    try:
        result = graph.invoke(state, config=RUN_CONFIG)
    except (ModelOutputError, ModelCallTimeout) as exc:
        print(f"Run failed: {exc}", file=sys.stderr)
        return 1

    write_run(result, out_dir)
    status = result.get("status", "")
    print(f"Status: {status}  (model calls: {result.get('model_calls', 0)}, "
          f"opencode runs: {result.get('opencode_runs', 0)})")
    for decision in result.get("decisions", []):
        print(f"  - {decision}")
    if status == "needs_operator":
        print("\nThe team lead needs answers before continuing:")
        for question in (result.get("plan") or {}).get("questions", []):
            print(f"  ? {question}")
        print('Rerun with --answers "..."')
    if result.get("worktree"):
        print(f"\nBuild branch {result['branch']} in {result['worktree']} (nothing committed).")
        print(f"  Review:  git -C \"{result['worktree']}\" diff HEAD")
        print(f"  Discard: git -C \"{args.repo}\" worktree remove --force \"{result['worktree']}\" "
              f"&& git -C \"{args.repo}\" branch -D {result['branch']}")
    print(f"\nArtifacts: {out_dir}")
    return EXIT_CODES.get(status, 1)


if __name__ == "__main__":
    sys.exit(main())

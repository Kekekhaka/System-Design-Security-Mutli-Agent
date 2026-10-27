"""Revise an existing system's design with the designer + security loop.

Takes the system's current architecture and API/route spec (markdown) plus a
change brief listing what to change, what to keep and what to defer. The
designer produces a revised design, the security auditor reviews it, and the
loop repeats until the audit approves or MAX_REVISIONS is reached. This produces
a revised DESIGN, not code. Whatever you point it at is sent to the configured
model provider (OpenRouter), so use material you are allowed to share.

Usage:
    python scripts/revise_system.py reviews/digital-student-card --brief reviews/digital-student-card/change-brief.md

A folder must contain architecture.md and api-spec.md. Writes architecture.md,
api-spec.md, security-report.md and manifest.json to <folder>/revised (or --out).

Exit codes: 0 approved, 1 could not run, 2 finished without approval.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.multi_agent_graph import build_multi_agent, initial_state  # noqa: E402
from src.agents.nodes import is_approved  # noqa: E402
from src.agents.revision import build_revision_requirements  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core.llm import ModelCallTimeout, ModelOutputError, get_llm  # noqa: E402


def read_document(path: Path, label: str) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise SystemExit(f"Cannot read the {label} at {path}: {exc}")
    if not text:
        raise SystemExit(f"The {label} at {path} is empty.")
    return text


def write(path: Path, content: str) -> None:
    path.write_text((content or "").rstrip() + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Revise an existing system's design with the designer + security loop."
    )
    parser.add_argument("folder", help="Folder with architecture.md and api-spec.md")
    parser.add_argument("--brief", required=True, help="Change brief (markdown)")
    parser.add_argument("--out", help="Output folder (default: <folder>/revised)")
    args = parser.parse_args(argv)

    folder = Path(args.folder)
    try:
        architecture = read_document(folder / "architecture.md", "architecture document")
        api_spec = read_document(folder / "api-spec.md", "API spec")
        brief = read_document(Path(args.brief), "change brief")
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    out_dir = Path(args.out) if args.out else folder / "revised"
    out_dir.mkdir(parents=True, exist_ok=True)

    requirements = build_revision_requirements(architecture, api_spec, brief)
    graph = build_multi_agent(llm=get_llm())

    started = time.time()
    final: dict = {}
    designs = 0
    print(f"Revising {folder.name} ({len(architecture) + len(api_spec):,} chars of design, "
          f"{len(brief):,} chars of brief) with {settings.OPENROUTER_MODEL}; "
          f"up to {settings.MAX_REVISIONS} design/audit rounds")
    try:
        for update in graph.stream(initial_state(requirements), stream_mode="updates"):
            for node, values in update.items():
                final.update(values or {})
                # Save every round as it completes, so a stall or crash loses nothing.
                round_dir = out_dir / f"round-{max(designs, 1) if node == 'security' else designs + 1}"
                if node == "designer":
                    round_dir.mkdir(exist_ok=True)
                    write(round_dir / "architecture.md", values.get("architecture"))
                    write(round_dir / "api-spec.md", values.get("api_spec"))
                elif node == "security":
                    round_dir.mkdir(exist_ok=True)
                    write(round_dir / "security-report.md", values.get("security_findings"))
                if node == "designer":
                    designs += 1
                    print(f"  [{time.time() - started:5.0f}s] designer finished round {designs}", flush=True)
                elif node == "security":
                    verdict = "APPROVED" if is_approved(values.get("security_findings")) else "changes requested"
                    print(f"  [{time.time() - started:5.0f}s] security audit {values.get('revision_count')}: {verdict}", flush=True)
    except (ModelOutputError, ModelCallTimeout) as exc:
        print(f"Revision failed: {exc}", file=sys.stderr)
        return 1

    approved = is_approved(final.get("security_findings"))
    write(out_dir / "architecture.md", final.get("architecture"))
    write(out_dir / "api-spec.md", final.get("api_spec"))
    write(out_dir / "security-report.md", final.get("security_findings"))
    write(
        out_dir / "manifest.json",
        json.dumps(
            {
                "revised_at": datetime.now(timezone.utc).isoformat(),
                "model": settings.OPENROUTER_MODEL,
                "provider_base_url": settings.OPENROUTER_BASE_URL,
                "source_folder": str(folder),
                "brief_file": str(args.brief),
                "approved": approved,
                "design_rounds": designs,
                "audits": final.get("revision_count"),
                "seconds": round(time.time() - started),
                "note": "A revised DESIGN generated from the existing design text and the "
                "brief. Nothing was built or run; the audit covers the design only.",
            },
            indent=2,
        ),
    )
    if not (final.get("api_spec") or "").strip():
        print("Warning: the designer returned no API spec section; check architecture.md.", file=sys.stderr)
    print(f"\n{'APPROVED' if approved else 'NOT APPROVED'} after {final.get('revision_count')} audit(s) "
          f"in {time.time() - started:.0f}s. Revised design: {out_dir}")
    return 0 if approved else 2


if __name__ == "__main__":
    sys.exit(main())

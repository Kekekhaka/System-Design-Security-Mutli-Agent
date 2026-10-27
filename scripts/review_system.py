"""Security-review an existing system's design documents.

Feeds an architecture description and an API/route spec (markdown) through the
scope -> analyze -> validate audit and writes the report next to them. Nothing
is redesigned here. The audit reads design text only: it never sees source
code, configuration values or infrastructure, so it can only find what the
documents describe. Whatever you point it at is sent to the configured model
provider (OpenRouter), so use material you are allowed to share.

Usage:
    python scripts/review_system.py reviews/digital-student-card
    python scripts/review_system.py --architecture a.md --api-spec b.md --out reviews/x

A folder must contain architecture.md and api-spec.md. Writes scope.md,
draft-findings.md, security-report.md and review-manifest.json to the output
folder (the input folder by default).

Exit codes: 0 approved, 1 could not run, 2 changes requested.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.nodes import is_approved  # noqa: E402
from src.agents.security.graph import build_security_subgraph  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core.llm import ModelCallTimeout, ModelOutputError, get_llm  # noqa: E402
from src.knowledge.retriever import load_default_retriever  # noqa: E402


def read_document(path: Path, label: str) -> str:
    try:
        # utf-8-sig: tolerate a BOM from Windows editors and PowerShell.
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise SystemExit(f"Cannot read the {label} at {path}: {exc}")
    if not text:
        raise SystemExit(f"The {label} at {path} is empty.")
    return text


def write(path: Path, content: str) -> None:
    path.write_text(content.rstrip() + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Security-review an existing system's design documents."
    )
    parser.add_argument("folder", nargs="?", help="Folder with architecture.md and api-spec.md")
    parser.add_argument("--architecture", help="Architecture description (markdown)")
    parser.add_argument("--api-spec", help="API or route spec (markdown)")
    parser.add_argument("--out", help="Output folder (default: the input folder)")
    args = parser.parse_args(argv)

    if args.folder:
        folder = Path(args.folder)
        architecture_path = Path(args.architecture) if args.architecture else folder / "architecture.md"
        api_spec_path = Path(args.api_spec) if args.api_spec else folder / "api-spec.md"
        out_dir = Path(args.out) if args.out else folder
    elif args.architecture and args.api_spec:
        architecture_path, api_spec_path = Path(args.architecture), Path(args.api_spec)
        out_dir = Path(args.out) if args.out else architecture_path.parent
    else:
        parser.error("give a folder, or both --architecture and --api-spec")

    try:
        architecture = read_document(architecture_path, "architecture document")
        api_spec = read_document(api_spec_path, "API spec")
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    retriever = load_default_retriever() if settings.SECURITY_RAG_ENABLED else None
    graph = build_security_subgraph(get_llm(temperature=0.0), retriever=retriever)

    started = time.time()
    final: dict = {}
    print(f"Reviewing {architecture_path.name} ({len(architecture):,} chars) and "
          f"{api_spec_path.name} ({len(api_spec):,} chars) with {settings.OPENROUTER_MODEL}")
    try:
        for update in graph.stream(
            {"architecture": architecture, "api_spec": api_spec}, stream_mode="updates"
        ):
            for node, values in update.items():
                final.update(values or {})
                print(f"  [{time.time() - started:5.0f}s] finished {node}", flush=True)
                if node == "scope":
                    write(out_dir / "scope.md", values["scope"])
                elif node == "analyze":
                    write(out_dir / "draft-findings.md", values["draft_findings"])
    except (ModelOutputError, ModelCallTimeout) as exc:
        print(f"Review failed: {exc}", file=sys.stderr)
        return 1

    findings = final["security_findings"]
    approved = is_approved(findings)
    write(out_dir / "security-report.md", findings)
    write(
        out_dir / "review-manifest.json",
        json.dumps(
            {
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "model": settings.OPENROUTER_MODEL,
                "provider_base_url": settings.OPENROUTER_BASE_URL,
                "architecture_file": str(architecture_path),
                "api_spec_file": str(api_spec_path),
                "approved": approved,
                "seconds": round(time.time() - started),
                "note": "Design-level review of the supplied documents only; code, "
                "configuration values and infrastructure were not seen.",
            },
            indent=2,
        ),
    )
    print(f"\nVerdict: {'APPROVED' if approved else 'CHANGES REQUESTED'} "
          f"({time.time() - started:.0f}s). Report: {out_dir / 'security-report.md'}")
    return 0 if approved else 2


if __name__ == "__main__":
    sys.exit(main())

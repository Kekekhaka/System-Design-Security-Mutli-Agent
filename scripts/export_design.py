"""Export a finished design as machine-readable specs.

Reads the JSON a design run returned (the `final` event of /project/design/stream,
saved as result.json) and writes openapi.yaml and schema.sql, plus the design
documents and a manifest, to a folder. Two LLM calls; requires OPENROUTER_API_KEY.

Usage:
    python scripts/export_design.py result.json
    python scripts/export_design.py result.json --out export/guest-house
    python scripts/export_design.py result.json --allow-unapproved

Exit codes: 0 clean, 1 refused or failed, 2 written but the checks flagged problems.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import settings  # noqa: E402
from src.core.llm import ModelCallTimeout, ModelOutputError, get_llm  # noqa: E402
from src.export.exporter import export_design, write_export  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export a finished design as openapi.yaml and schema.sql."
    )
    parser.add_argument("input", nargs="?", default="result.json")
    parser.add_argument(
        "--out",
        default=None,
        help="Output folder (default: export/<input name> under the project root).",
    )
    parser.add_argument(
        "--allow-unapproved",
        action="store_true",
        help="Export a design the security audit did not approve.",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    try:
        # utf-8-sig: PowerShell's Out-File -Encoding utf8 writes a BOM.
        design = json.loads(input_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read {input_path}: {exc}", file=sys.stderr)
        return 1

    if not design.get("approved") and not args.allow_unapproved:
        print(
            "Refusing to export: the security audit did not approve this design. "
            "Re-run the design, or pass --allow-unapproved.",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(args.out) if args.out else ROOT / "export" / input_path.stem
    try:
        llm = get_llm(
            temperature=0.0,
            max_tokens=settings.EXPORT_MAX_TOKENS,
            reasoning_effort=settings.EXPORT_REASONING_EFFORT or None,
        )
        result = export_design(llm, design)
    except (ValueError, ModelOutputError, ModelCallTimeout) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1

    for path in write_export(result, design, out_dir):
        print(f"wrote {path}")
    if result.flagged:
        print("\nThe checks flagged problems (also in manifest.json):", file=sys.stderr)
        for name, problems in result.problems.items():
            for problem in problems:
                print(f"  [{name}] {problem}", file=sys.stderr)
        return 2
    print("\nChecks passed. The output is generated from the design text; review before use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

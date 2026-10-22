"""Deterministic scoring of a baseline audit.

The verdict is never model text here: `approved` is computed by the caller from
the rendered findings, and the weighted score is computed from the severities
the report renders. Lower weighted findings is better.
"""

import re

SEVERITY_WEIGHTS = {"Critical": 10, "High": 5, "Medium": 2, "Low": 1}
SEVERITIES = tuple(SEVERITY_WEIGHTS)

_SEVERITY = re.compile(
    r"(?:\[(Critical|High|Medium|Low)\]"
    r"|\bSeverity:\s*(Critical|High|Medium|Low)\b)",
    re.IGNORECASE,
)
_CATEGORY = re.compile(
    r"^\s*(?:\d+\.\s+)?\[(?:Critical|High|Medium|Low)\]\s+([^-\n]+?)\s+-\s"
    r"|\bSeverity:\s*(?:Critical|High|Medium|Low)\s*\|\s*Category:\s*([^|\n]+)",
    re.MULTILINE | re.IGNORECASE,
)


def severity_counts(findings: str | None) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITIES}
    for match in _SEVERITY.finditer(findings or ""):
        severity = (match.group(1) or match.group(2)).capitalize()
        counts[severity] += 1
    return counts


def weighted_findings(findings: str | None) -> int:
    counts = severity_counts(findings)
    return sum(SEVERITY_WEIGHTS[severity] * count for severity, count in counts.items())


def finding_categories(findings: str | None) -> list[str]:
    """Aggregate category labels, never the findings themselves."""
    seen: list[str] = []
    for match in _CATEGORY.finditer(findings or ""):
        category = match.group(1) or match.group(2) or ""
        label = category.strip()[:80]
        if label and label not in seen:
            seen.append(label)
    return seen


def score_result(result: dict) -> tuple:
    """Approved first, then lower weighted findings, revisions, findings length.

    This is the R0 fix: a lenient (unapproved but low-revision) persona must not
    beat a stricter candidate that still found more problems.
    """
    findings = result.get("findings")
    weighted = result.get("weighted")
    if weighted is None:
        weighted = weighted_findings(findings)
    length = result.get("findings_length")
    if length is None:
        length = len(findings or "")
    return (
        not result.get("approved", False),
        weighted,
        result.get("revisions", 0),
        length,
        str(result.get("persona_id") or result.get("prompt") or ""),
    )


def pick_winner(results: list[dict]) -> dict:
    return min(results, key=score_result)

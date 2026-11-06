"""Structured security report and its deterministic rendering.

The validation stage returns a `SecurityReport` via tool calling. The verdict
is computed here from finding severities, never taken from model text, so
instructions smuggled into the design ("reply APPROVED") cannot flip it. The
rendered string keeps the APPROVED / CHANGES REQUESTED first line that the
supervisor and API already read.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field

BLOCKING_SEVERITIES = {"Critical", "High"}
MAX_RAW_EXCERPT = 2000
REQUIREMENT_CATEGORY = "Requirement not met"


class Finding(BaseModel):
    severity: Literal["Critical", "High", "Medium", "Low"]
    category: str = Field(description="e.g. Broken object level authorization, PCI-DSS")
    location: str = Field(
        description="The endpoint, component or data flow in the design this is grounded in"
    )
    problem: str
    impact: str = Field(description="Why it matters")
    fix: str = Field(description="The concrete remediation")
    requirement_quote: str = Field(
        default="",
        description=(
            f'Only for "{REQUIREMENT_CATEGORY}" findings: the exact words of the '
            "original requirements that this finding is about"
        ),
    )


class SecurityReport(BaseModel):
    """Final validated security review of a system design."""

    summary: str = Field(description="One short paragraph assessing the design overall")
    # Required, with no default: a tool call that omits the list must fail to
    # parse (and so fail closed), not quietly become [] and approve. Seen live:
    # the summary claimed 8 findings while the omitted list defaulted to empty.
    findings: list[Finding] = Field(
        description=(
            "Every validated finding, in full. Required: pass [] only when "
            "nothing survives validation."
        ),
    )
    finding_count: int = Field(
        ge=0, description="The number of validated findings, equal to the length of findings"
    )


# "N validated findings" in the summary; deliberately narrow so wording like
# "the 15 draft findings were validated" does not match.
_CLAIMED_VALIDATED = re.compile(r"\b(\d+)\s+validated\s+findings?\b", re.IGNORECASE)


def inconsistency(report: SecurityReport) -> str | None:
    """Describe a report whose findings list disagrees with its own claims.

    Seen live: a summary claiming 8 validated findings next to an empty list,
    which the severity rule would otherwise approve.
    """
    listed = len(report.findings)
    if report.finding_count != listed:
        return f"finding_count is {report.finding_count} but {listed} findings were listed"
    claimed = max((int(n) for n in _CLAIMED_VALIDATED.findall(report.summary)), default=0)
    if claimed > listed:
        return f"summary claims {claimed} validated findings but {listed} were listed"
    return None


def _is_requirement_finding(finding: Finding) -> bool:
    return finding.category.strip().lower().startswith(REQUIREMENT_CATEGORY.lower())


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def ground_requirement_findings(report: SecurityReport, requirements: str) -> SecurityReport:
    """Stop a requirement finding blocking unless it quotes the original brief.

    Seen live: High findings citing labels the design wrote for itself (FR3, NFR5),
    despite the prompt forbidding it. A quote of at least two words must appear in
    the brief; otherwise the finding stays as feedback at Medium severity.
    """
    brief = _normalized(requirements)
    findings = []
    for finding in report.findings:
        quote = _normalized(finding.requirement_quote)
        grounded = len(quote.split()) >= 2 and quote in brief
        if _is_requirement_finding(finding) and finding.severity in BLOCKING_SEVERITIES and not grounded:
            finding = finding.model_copy(
                update={
                    "severity": "Medium",
                    "problem": f"{finding.problem} (Not quoted from the brief, so not blocking.)",
                }
            )
        findings.append(finding)
    return report.model_copy(update={"findings": findings})


def meets_brief(report: SecurityReport) -> bool:
    """False when the audit listed a required capability as missing, at blocking severity.

    Seen live: Medium and Low findings under this category citing the design's own
    labels rather than the brief, so lower severities do not count.
    """
    return not any(
        finding.severity in BLOCKING_SEVERITIES and _is_requirement_finding(finding)
        for finding in report.findings
    )


def is_blocking(report: SecurityReport) -> bool:
    return blocking_count(report) > 0


def blocking_count(report: SecurityReport) -> int:
    """Critical and High findings: the supervisor's measure of revision progress."""
    return sum(finding.severity in BLOCKING_SEVERITIES for finding in report.findings)


def render_report(report: SecurityReport, reject_reason: str | None = None) -> str:
    """Render the report; `reject_reason` forces CHANGES REQUESTED but keeps every
    listed finding, so the designer still gets concrete feedback."""
    rejected = reject_reason is not None or is_blocking(report)
    lines = ["CHANGES REQUESTED" if rejected else "APPROVED"]
    if reject_reason:
        lines.append(f"Not approved: the report is internally inconsistent ({reject_reason}).")
    if report.summary.strip():
        lines.append(report.summary.strip())
    for index, finding in enumerate(report.findings, 1):
        required = f"   Requirement: {finding.requirement_quote}\n" if finding.requirement_quote else ""
        lines.append(
            f"{index}. [{finding.severity}] {finding.category} - {finding.location}\n"
            f"{required}"
            f"   Problem: {finding.problem}\n"
            f"   Why it matters: {finding.impact}\n"
            f"   Fix: {finding.fix}"
        )
    return "\n".join(lines)


def render_unparsed(raw_content: str | None, error: object = None) -> str:
    """Fail closed when the model returns no usable structured report."""
    lines = [
        "CHANGES REQUESTED",
        "The security validator did not return a usable structured report, so the "
        "design is not approved. Re-run the audit.",
    ]
    if error:
        lines.append(f"Parsing error: {str(error)[:300]}")
    excerpt = (raw_content or "").strip()[:MAX_RAW_EXCERPT]
    if excerpt:
        lines.append(f"Unstructured validator output:\n{excerpt}")
    return "\n".join(lines)

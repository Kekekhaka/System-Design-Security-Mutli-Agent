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


class Finding(BaseModel):
    severity: Literal["Critical", "High", "Medium", "Low"]
    category: str = Field(description="e.g. Broken object level authorization, PCI-DSS")
    location: str = Field(
        description="The endpoint, component or data flow in the design this is grounded in"
    )
    problem: str
    impact: str = Field(description="Why it matters")
    fix: str = Field(description="The concrete remediation")


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


def is_blocking(report: SecurityReport) -> bool:
    return any(finding.severity in BLOCKING_SEVERITIES for finding in report.findings)


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
        lines.append(
            f"{index}. [{finding.severity}] {finding.category} - {finding.location}\n"
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

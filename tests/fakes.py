"""Offline LLM fakes shared by the test suite."""

from langchain_core.messages import AIMessage

from src.agents.security.report import Finding, SecurityReport

# A scripted response equal to this makes the structured call return no
# parsed report, as when a model ignores the tool and answers in prose.
NO_STRUCTURED_OUTPUT = "<<no structured output>>"


def report_from_text(text: str) -> SecurityReport:
    """Map a scripted 'APPROVED ...' / 'CHANGES REQUESTED ...' reply to a report."""
    lines = (text or "").strip().splitlines()
    head = lines[0].strip().upper() if lines else ""
    body = "\n".join(lines[1:]).strip()
    if head.startswith("APPROVED"):
        return SecurityReport(summary=body, findings=[], finding_count=0)
    return SecurityReport(
        summary="Scripted rejection.",
        findings=[
            Finding(
                severity="Critical",
                category="Scripted",
                location="design",
                problem=body or text,
                impact="n/a",
                fix="n/a",
            )
        ],
        finding_count=1,
    )


class StructuredFake:
    """Mimics `llm.with_structured_output(..., include_raw=...)`."""

    def __init__(self, llm, to_report, include_raw: bool) -> None:
        self.llm = llm
        self.to_report = to_report
        self.include_raw = include_raw

    def invoke(self, messages, **kwargs):
        raw = self.llm.invoke(messages)
        parsed = None if raw.content == NO_STRUCTURED_OUTPUT else self.to_report(raw.content)
        if self.include_raw:
            return {"raw": raw, "parsed": parsed, "parsing_error": None}
        return parsed


class ScriptedLLM:
    """Returns canned responses in order; repeats the last one when exhausted."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list] = []

    def invoke(self, messages, **kwargs) -> AIMessage:
        index = len(self.calls)
        self.calls.append(messages)
        return AIMessage(content=self.responses[min(index, len(self.responses) - 1)])

    def with_structured_output(self, schema, **kwargs) -> StructuredFake:
        return StructuredFake(self, report_from_text, kwargs.get("include_raw", False))

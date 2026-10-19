from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from src.agents.security.prompts import (
    SCOPE_SYSTEM_PROMPT,
    SECURITY_SYSTEM_PROMPT,
    VALIDATE_SYSTEM_PROMPT,
    build_analyze_prompt,
    build_scope_prompt,
    build_validate_prompt,
)
from src.agents.security.report import (
    SecurityReport,
    inconsistency,
    render_report,
    render_unparsed,
)
from src.core.config import settings
from src.core.llm import call_with_deadline, invoke_checked
from src.knowledge.retriever import format_references


class SecurityState(TypedDict):
    architecture: str
    api_spec: str
    references: str
    scope: str
    draft_findings: str
    security_findings: str


def build_security_subgraph(llm, system_prompt: str | None = None, retriever=None):
    """Compile the scope -> analyze -> validate security review pipeline.

    `system_prompt` is the reviewer persona used by the analysis stage and
    defaults to SECURITY_SYSTEM_PROMPT. `retriever` injects grounded control
    guidance into the scoping and analysis stages. The validation stage returns
    a structured SecurityReport and the verdict is derived from severities.
    """
    persona = system_prompt or SECURITY_SYSTEM_PROMPT

    def scope_node(state: SecurityState) -> dict:
        references = ""
        if retriever is not None:
            query = f"{state.get('architecture') or ''}\n{state.get('api_spec') or ''}"
            references = format_references(
                retriever.retrieve(query, settings.SECURITY_RAG_TOP_K)
            )
        scope = invoke_checked(
            llm,
            [
                SystemMessage(content=SCOPE_SYSTEM_PROMPT),
                HumanMessage(
                    content=build_scope_prompt(
                        state.get("architecture"), state.get("api_spec"), references
                    )
                ),
            ],
            "security scope",
        )
        return {"references": references, "scope": scope}

    def analyze_node(state: SecurityState) -> dict:
        # An empty draft must never reach validation as "no findings".
        draft = invoke_checked(
            llm,
            [
                SystemMessage(content=persona),
                HumanMessage(
                    content=build_analyze_prompt(
                        state.get("architecture"),
                        state.get("api_spec"),
                        state.get("scope", ""),
                        state.get("references", ""),
                    )
                ),
            ],
            "security analysis",
        )
        return {"draft_findings": draft}

    # Tool calling is the most widely supported structured mode across
    # OpenRouter providers; include_raw keeps parse failures from raising.
    validator = llm.with_structured_output(
        SecurityReport, method="function_calling", include_raw=True
    )

    def validate_node(state: SecurityState) -> dict:
        messages = [
            SystemMessage(content=VALIDATE_SYSTEM_PROMPT),
            HumanMessage(
                content=build_validate_prompt(
                    state.get("architecture"),
                    state.get("api_spec"),
                    state.get("draft_findings", ""),
                )
            ),
        ]
        output = call_with_deadline(lambda: validator.invoke(messages), "security validation")
        report = output.get("parsed")
        if not isinstance(report, SecurityReport):
            raw = output.get("raw")
            finish = (getattr(raw, "response_metadata", None) or {}).get("finish_reason")
            error = output.get("parsing_error") or (
                f"output truncated (finish_reason={finish!r}); raise LLM_MAX_TOKENS"
                if finish == "length"
                else None
            )
            return {
                "security_findings": render_unparsed(getattr(raw, "content", None), error)
            }
        # An inconsistent report (count or summary disagreeing with the list)
        # is never approved, but its listed findings are kept as feedback.
        return {"security_findings": render_report(report, reject_reason=inconsistency(report))}

    builder = StateGraph(SecurityState)
    builder.add_node("scope", scope_node)
    builder.add_node("analyze", analyze_node)
    builder.add_node("validate", validate_node)
    builder.add_edge(START, "scope")
    builder.add_edge("scope", "analyze")
    builder.add_edge("analyze", "validate")
    builder.add_edge("validate", END)
    return builder.compile()

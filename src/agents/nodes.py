from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.multi_agent_state import ProjectState
from src.agents.prompts import DESIGNER_SYSTEM_PROMPT, build_designer_prompt
from src.agents.security.graph import build_security_subgraph
from src.core.config import settings
from src.core.llm import invoke_checked
from src.knowledge.retriever import format_references

ARCHITECTURE_MARKER = "===ARCHITECTURE==="
API_SPEC_MARKER = "===API SPEC==="


def parse_design_sections(text: str) -> tuple[str, str]:
    """Split the designer's output into (architecture, api_spec)."""
    if ARCHITECTURE_MARKER not in text:
        return text.strip(), ""
    remainder = text.split(ARCHITECTURE_MARKER, 1)[1]
    if API_SPEC_MARKER in remainder:
        architecture, api_spec = remainder.split(API_SPEC_MARKER, 1)
        return architecture.strip(), api_spec.strip()
    return remainder.strip(), ""


def is_approved(findings: str | None) -> bool:
    if not findings:
        return False
    first_line = findings.strip().splitlines()[0].strip().upper()
    return first_line.startswith("APPROVED")


def make_designer_node(llm, retriever=None):
    def designer_node(state: ProjectState) -> dict:
        requirements = state.get("requirements") or ""
        references = ""
        if retriever is not None and requirements:
            references = format_references(
                retriever.retrieve(requirements, k=settings.DESIGN_RAG_TOP_K)
            )
        prompt = build_designer_prompt(
            requirements,
            state.get("security_findings"),
            references,
        )
        content = invoke_checked(
            llm,
            [
                SystemMessage(content=DESIGNER_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ],
            "designer",
        )
        architecture, api_spec = parse_design_sections(content)
        # The feedback has been consumed by this revision; clear it so the
        # supervisor routes back to the security audit.
        return {
            "architecture": architecture,
            "api_spec": api_spec,
            "security_findings": None,
        }

    return designer_node


def make_security_node(llm, system_prompt: str | None = None, retriever=None):
    subgraph = build_security_subgraph(
        llm, system_prompt=system_prompt, retriever=retriever
    )

    def security_node(state: ProjectState) -> dict:
        result = subgraph.invoke(
            {
                "architecture": state.get("architecture") or "",
                "api_spec": state.get("api_spec") or "",
            }
        )
        history = list(state.get("blocking_history") or [])
        history.append(result.get("blocking_count"))
        return {
            "security_findings": result["security_findings"],
            "revision_count": state.get("revision_count", 0) + 1,
            "blocking_history": history,
        }

    return security_node


def not_improving(history: list[int | None] | None) -> bool:
    """True when the last revision left at least as many Critical + High findings.

    Only two parsed rounds are compared: an unparsed report (None) says nothing
    about the design, and a round with no blocking findings is not stuck.
    """
    if not history or len(history) < 2:
        return False
    previous, latest = history[-2], history[-1]
    if previous is None or latest is None:
        return False
    return latest > 0 and latest >= previous


def supervisor_node(state: ProjectState) -> dict:
    """Route between Designer and Security Auditor. Pure routing, no LLM."""
    if not state.get("architecture"):
        return {"next_agent": "designer"}

    findings = state.get("security_findings")
    if not findings:
        return {"next_agent": "security"}

    limit = settings.MAX_REVISIONS
    if state.get("max_revisions") is not None:
        limit = max(0, min(state["max_revisions"], limit))
    if is_approved(findings) or state.get("revision_count", 0) >= limit:
        return {"next_agent": "FINISH"}
    # A revision that did not reduce the blocking findings is unlikely to be
    # rescued by the next one; stop and leave the rounds unspent.
    if settings.STOP_WHEN_NOT_IMPROVING and not_improving(state.get("blocking_history")):
        return {"next_agent": "FINISH", "stop_reason": "no_progress"}

    return {"next_agent": "designer"}

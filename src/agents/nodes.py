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
        return {
            "security_findings": result["security_findings"],
            "revision_count": state.get("revision_count", 0) + 1,
        }

    return security_node


def supervisor_node(state: ProjectState) -> dict:
    """Route between Designer and Security Auditor. Pure routing, no LLM."""
    if not state.get("architecture"):
        return {"next_agent": "designer"}

    findings = state.get("security_findings")
    if not findings:
        return {"next_agent": "security"}

    if is_approved(findings) or state.get("revision_count", 0) >= settings.MAX_REVISIONS:
        return {"next_agent": "FINISH"}

    return {"next_agent": "designer"}

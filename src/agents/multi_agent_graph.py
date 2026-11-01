from functools import lru_cache

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from src.agents.multi_agent_state import ProjectState
from src.agents.nodes import make_designer_node, make_security_node, supervisor_node
from src.core.config import settings
from src.core.llm import get_llm
from src.knowledge.retriever import load_default_retriever, load_design_retriever


def build_multi_agent(
    llm=None,
    checkpointer=None,
    security_prompt: str | None = None,
    security_retriever=None,
    design_retriever=None,
):
    """Wire supervisor, designer and security into a StateGraph.

    `security_prompt` swaps the security auditor's persona system prompt;
    it defaults to SECURITY_SYSTEM_PROMPT. `security_retriever` injects
    AppSec control guidance; when omitted it loads the repo corpus unless
    SECURITY_RAG_ENABLED is false. `design_retriever` injects architecture
    reference patterns into the designer under the same rule via
    DESIGN_RAG_ENABLED.
    """
    llm = llm if llm is not None else get_llm()
    if security_retriever is None and settings.SECURITY_RAG_ENABLED:
        security_retriever = load_default_retriever()
    if design_retriever is None and settings.DESIGN_RAG_ENABLED:
        design_retriever = load_design_retriever()

    builder = StateGraph(ProjectState)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("designer", make_designer_node(llm, retriever=design_retriever))
    builder.add_node(
        "security",
        make_security_node(
            llm, system_prompt=security_prompt, retriever=security_retriever
        ),
    )

    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        lambda state: state["next_agent"],
        {"designer": "designer", "security": "security", "FINISH": END},
    )
    builder.add_edge("designer", "supervisor")
    builder.add_edge("security", "supervisor")

    return builder.compile(checkpointer=checkpointer)


@lru_cache(maxsize=1)
def get_multi_agent_app():
    """Process-wide app; keeps per-session threads in memory."""
    return build_multi_agent(checkpointer=InMemorySaver())


def initial_state(requirements: str, max_revisions: int | None = None) -> dict:
    """Fresh state, so repeated runs on the same thread_id never reuse old fields.

    `max_revisions` caps this run below MAX_REVISIONS (harness candidates use it).
    """
    return {
        "requirements": requirements,
        "architecture": None,
        "api_spec": None,
        "security_findings": None,
        "revision_count": 0,
        "max_revisions": max_revisions,
        "blocking_history": [],
        "stop_reason": None,
    }

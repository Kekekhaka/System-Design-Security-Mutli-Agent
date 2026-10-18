from langchain_core.messages import AIMessage

from src.agents.nodes import make_designer_node
from src.knowledge.retriever import load_design_retriever

DESIGN = "===ARCHITECTURE===\nArch\n===API SPEC===\nSpec"


class RecordingLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[list] = []

    def invoke(self, messages, **kwargs) -> AIMessage:
        self.calls.append(messages)
        return AIMessage(content=self.response)


def test_design_corpus_loads_and_ranks_payment_patterns():
    retriever = load_design_retriever()
    assert retriever is not None
    top = retriever.retrieve("payment ledger reconciliation", k=1)
    assert top[0].id == "06-tenancy-and-payment-flows"


def test_design_corpus_ranks_pagination_first():
    retriever = load_design_retriever()
    top = retriever.retrieve("cursor pagination page size", k=1)
    assert top[0].id == "01-api-design-checklist"


def test_designer_prompt_includes_references():
    llm = RecordingLLM(DESIGN)
    node = make_designer_node(llm, retriever=load_design_retriever())
    result = node({"requirements": "design a payment gateway"})

    prompt = llm.calls[0][1].content
    assert "Reference patterns from the architecture knowledge base" in prompt
    assert "ledger" in prompt.lower()
    assert result["architecture"] == "Arch"
    assert result["api_spec"] == "Spec"


def test_designer_without_requirements_skips_retrieval():
    llm = RecordingLLM(DESIGN)
    node = make_designer_node(llm, retriever=load_design_retriever())
    node({"requirements": ""})

    prompt = llm.calls[0][1].content
    assert "Reference patterns" not in prompt

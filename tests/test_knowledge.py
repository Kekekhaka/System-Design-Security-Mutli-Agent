from pathlib import Path

import pytest

from src.knowledge.retriever import (
    Entry,
    LexicalRetriever,
    format_references,
    load_default_retriever,
)


def test_default_corpus_loads():
    retriever = load_default_retriever()
    assert retriever is not None
    assert len(retriever.entries) >= 8


def test_ssrf_query_ranks_ssrf_entry():
    retriever = load_default_retriever()
    top = retriever.retrieve("server side fetch of webhook url ssrf metadata", k=1)[0]
    assert "ssrf" in top.id


def test_card_data_query_ranks_pci_entry():
    retriever = load_default_retriever()
    top = retriever.retrieve(
        "card number cvv stored in database tokenization payments", k=1
    )[0]
    assert "pci" in top.id


def test_empty_query_is_deterministic_and_bounded():
    retriever = load_default_retriever()
    first = retriever.retrieve("", k=3)
    second = retriever.retrieve("", k=3)
    assert [entry.id for entry in first] == [entry.id for entry in second]
    assert len(first) == 3


def test_from_directory_raises_on_missing_entries(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        LexicalRetriever.from_directory(tmp_path)


def test_format_references_contains_titles():
    retriever = LexicalRetriever.from_entries(
        [Entry(id="x", title="Test Control", text="Do the thing.")]
    )
    block = format_references(retriever.retrieve("thing", k=1))
    assert "[Test Control]" in block
    assert format_references([]) == ""


def test_unknown_rag_backend_raises(monkeypatch, tmp_path: Path):
    from src.core.config import settings
    from src.knowledge.retriever import build_retriever

    (tmp_path / "a.md").write_text("# A\nalpha", encoding="utf-8")
    monkeypatch.setattr(settings, "RAG_BACKEND", "pinecone")
    with pytest.raises(ValueError, match="RAG_BACKEND"):
        build_retriever(tmp_path, collection="x")


def make_hashing_embedding():
    """Deterministic offline bag-of-words embedding, so the test never downloads a model."""
    import math

    from chromadb.api.types import EmbeddingFunction

    from src.knowledge.retriever import tokenize

    class HashingEmbedding(EmbeddingFunction):
        def __init__(self) -> None:
            pass

        def __call__(self, input):
            vectors = []
            for text in input:
                vector = [0.0] * 256
                for token in tokenize(text):
                    vector[sum(map(ord, token)) * 31 % 256] += 1.0
                norm = math.sqrt(sum(v * v for v in vector)) or 1.0
                vectors.append([v / norm for v in vector])
            return vectors

        @staticmethod
        def name() -> str:
            return "test-hashing"

        def get_config(self) -> dict:
            return {}

        @staticmethod
        def build_from_config(config):
            return HashingEmbedding()

    return HashingEmbedding()


def test_chroma_retriever_ranks_and_syncs(tmp_path: Path):
    pytest.importorskip("chromadb")
    from src.knowledge.retriever import DEFAULT_KNOWLEDGE_DIR, ChromaRetriever, _load_entry

    embedding = make_hashing_embedding()
    entries = [_load_entry(p) for p in sorted(DEFAULT_KNOWLEDGE_DIR.glob("*.md"))]
    retriever = ChromaRetriever(entries, "appsec-test", tmp_path / "chroma", embedding)

    top = retriever.retrieve("card number cvv stored in database tokenization payments", k=1)
    assert "pci" in top[0].id
    assert len(retriever.retrieve("", k=3)) == 3
    assert retriever.retrieve("anything", k=0) == []

    # Re-opening the same collection with a smaller corpus deletes stale entries.
    only = Entry(id="only", title="Only", text="rate limiting")
    shrunk = ChromaRetriever([only], "appsec-test", tmp_path / "chroma", embedding)
    assert [entry.id for entry in shrunk.retrieve("anything", k=5)] == ["only"]


def test_build_retriever_selects_chroma(monkeypatch, tmp_path: Path):
    pytest.importorskip("chromadb")
    from src.core.config import settings
    from src.knowledge import retriever as retriever_module

    built = {}

    class RecordingChroma:
        def __init__(self, entries, collection, path):
            built.update(ids=[e.id for e in entries], collection=collection, path=Path(path))

    (tmp_path / "a.md").write_text("# A\nalpha", encoding="utf-8")
    monkeypatch.setattr(settings, "RAG_BACKEND", "chroma")
    monkeypatch.setattr(settings, "CHROMA_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(retriever_module, "ChromaRetriever", RecordingChroma)

    retriever_module.build_retriever(tmp_path, collection="appsec")
    assert built == {"ids": ["a"], "collection": "appsec", "path": tmp_path / "store"}

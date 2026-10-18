"""Retrieval over the curated AppSec and system-design knowledge corpora.

Two backends share one `retrieve(query, k) -> list[Entry]` surface, picked by
RAG_BACKEND:

- "bm25" (default): lexical BM25. The corpus is small and keyword-dense
  (control names, endpoint patterns), so BM25 is deterministic, needs zero
  extra dependencies or model downloads, and keeps CI offline.
- "chroma": ChromaDB embeddings persisted under CHROMA_DIR. Install
  requirements-chroma.txt; Chroma's default embedding model downloads on
  first use. Worth it once the corpus grows or queries get more paraphrased.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.core.config import settings

TOKEN_RE = re.compile(r"[a-z0-9]+")
DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge" / "appsec"
DEFAULT_DESIGN_KNOWLEDGE_DIR = (
    Path(__file__).resolve().parents[2] / "knowledge" / "system-design"
)
DEFAULT_CHROMA_DIR = Path(__file__).resolve().parents[2] / ".chroma"


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    text: str


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def _load_entry(path: Path) -> Entry:
    text = path.read_text(encoding="utf-8").strip()
    first_line = text.splitlines()[0] if text else ""
    title = first_line.lstrip("# ").strip() if first_line.startswith("#") else path.stem
    return Entry(id=path.stem, title=title, text=text)


class LexicalRetriever:
    def __init__(self, entries: list[Entry], k1: float = 1.5, b: float = 0.75) -> None:
        self.entries = list(entries)
        self.k1 = k1
        self.b = b
        self._docs = [tokenize(f"{entry.title} {entry.text}") for entry in self.entries]
        self._lengths = [len(doc) for doc in self._docs]
        self._avg_len = (sum(self._lengths) / len(self._lengths)) if self._docs else 0.0
        doc_freq: Counter[str] = Counter()
        for doc in self._docs:
            doc_freq.update(set(doc))
        total = len(self._docs)
        self._idf = {
            token: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for token, freq in doc_freq.items()
        }

    @classmethod
    def from_directory(cls, path: Path | str) -> "LexicalRetriever":
        directory = Path(path)
        paths = sorted(directory.glob("*.md"))
        if not paths:
            raise FileNotFoundError(f"No .md entries found in {directory}")
        return cls([_load_entry(p) for p in paths])

    @classmethod
    def from_entries(cls, entries: list[Entry]) -> "LexicalRetriever":
        return cls(entries)

    def _score(self, doc: list[str], query_tokens: list[str]) -> float:
        counts = Counter(doc)
        length = len(doc)
        score = 0.0
        for token in query_tokens:
            freq = counts.get(token, 0)
            if not freq:
                continue
            denominator = freq + self.k1 * (1 - self.b + self.b * length / self._avg_len)
            score += self._idf.get(token, 0.0) * freq * (self.k1 + 1) / denominator
        return score

    def retrieve(self, query: str, k: int = 3) -> list[Entry]:
        query_tokens = tokenize(query or "")
        scored = [
            (self._score(doc, query_tokens), index)
            for index, doc in enumerate(self._docs)
        ]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [self.entries[index] for _, index in scored[:k]]


class ChromaRetriever:
    """Embedding retrieval via a persistent ChromaDB collection.

    The collection is synced with `entries` on construction: entries are
    upserted and ids no longer in the corpus are deleted, so editing the
    markdown files is still the only step needed to change the corpus.
    `embedding_function` overrides Chroma's default model, e.g. where its
    download is blocked or a hosted embedding model is preferred.
    """

    def __init__(
        self,
        entries: list[Entry],
        collection: str,
        path: Path | str,
        embedding_function=None,
    ) -> None:
        import chromadb  # optional dependency: requirements-chroma.txt

        self.entries = list(entries)
        self._by_id = {entry.id: entry for entry in self.entries}
        client = chromadb.PersistentClient(path=str(path))
        options = {} if embedding_function is None else {"embedding_function": embedding_function}
        self._collection = client.get_or_create_collection(collection, **options)
        stale = set(self._collection.get()["ids"]) - set(self._by_id)
        if stale:
            self._collection.delete(ids=sorted(stale))
        self._collection.upsert(
            ids=[entry.id for entry in self.entries],
            documents=[f"{entry.title}\n{entry.text}" for entry in self.entries],
            metadatas=[{"title": entry.title} for entry in self.entries],
        )

    def retrieve(self, query: str, k: int = 3) -> list[Entry]:
        if k <= 0 or not self.entries:
            return []
        result = self._collection.query(
            query_texts=[query or " "], n_results=min(k, len(self.entries))
        )
        return [self._by_id[i] for i in result["ids"][0] if i in self._by_id]


def format_references(entries: list[Entry]) -> str:
    if not entries:
        return ""
    blocks = [f"[{entry.title}]\n{entry.text}" for entry in entries]
    return "\n\n".join(blocks)


def build_retriever(directory: Path | str, collection: str):
    """Build the RAG_BACKEND retriever for a directory of .md entries, or None if empty."""
    paths = sorted(Path(directory).glob("*.md"))
    if not paths:
        return None
    entries = [_load_entry(p) for p in paths]
    backend = settings.RAG_BACKEND.strip().lower()
    if backend == "bm25":
        return LexicalRetriever(entries)
    if backend == "chroma":
        path = Path(settings.CHROMA_DIR) if settings.CHROMA_DIR else DEFAULT_CHROMA_DIR
        return ChromaRetriever(entries, collection=collection, path=path)
    raise ValueError(f"Unknown RAG_BACKEND {settings.RAG_BACKEND!r}; use 'bm25' or 'chroma'.")


@lru_cache(maxsize=1)
def load_default_retriever():
    directory = Path(settings.KNOWLEDGE_DIR) if settings.KNOWLEDGE_DIR else DEFAULT_KNOWLEDGE_DIR
    return build_retriever(directory, collection="appsec")


@lru_cache(maxsize=1)
def load_design_retriever():
    directory = (
        Path(settings.DESIGN_KNOWLEDGE_DIR)
        if settings.DESIGN_KNOWLEDGE_DIR
        else DEFAULT_DESIGN_KNOWLEDGE_DIR
    )
    return build_retriever(directory, collection="system-design")

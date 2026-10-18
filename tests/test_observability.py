from src.core import observability
from src.core.config import settings


def test_no_callbacks_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")
    assert observability.get_langfuse_callbacks() == []

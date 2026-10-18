import os

from src.core.config import settings


def get_langfuse_callbacks() -> list:
    """Return Langfuse LangChain callbacks, or [] when unconfigured/uninstalled.

    Keys come from settings (.env); they are mirrored into the environment so
    both langfuse SDK generations (langfuse.langchain / langfuse.callback)
    pick them up without explicit client construction.
    """
    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        return []

    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.LANGFUSE_PUBLIC_KEY)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.LANGFUSE_SECRET_KEY)
    os.environ.setdefault("LANGFUSE_HOST", settings.LANGFUSE_HOST)

    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        try:
            from langfuse.callback import CallbackHandler
        except ImportError:
            return []

    return [CallbackHandler()]

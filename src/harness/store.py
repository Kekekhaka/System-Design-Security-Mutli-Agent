"""Persistent persona library and run history for Harness2.

The store is a small JSON document, written atomically (temp file, fsync,
os.replace). A missing or empty store starts from the seeds. A corrupt store is
moved aside as ``<name>.bad`` and the seeds are used, so a bad file can never
stop the harness or be silently trusted.

When ``HARNESS_HMAC_KEY`` is set, the store body and each history line are
HMAC-SHA256 authenticated. A failed check quarantines the store. Without a key
the files are unsigned; this is logged once, never silently.
"""

import hashlib
import hmac as hmac_lib
import json
import logging
import os
import tempfile
from pathlib import Path

from src.core.config import settings
from src.harness.personas import ensure_seed, validate_persona
from src.harness.seeds import utc_now

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
HISTORY_SCHEMA_VERSION = 1
_WARNED_UNSIGNED = False


class StoreError(RuntimeError):
    """The store cannot be trusted and must not be used."""


class StoreVersionError(StoreError):
    """The store was written by a newer harness."""


class DuplicateRunId(StoreError):
    """A run id may only be appended once."""


class PersonaSecurityError(StoreError):
    """A persona is unsafe or invalid at consumption time."""


def default_store_path() -> Path:
    return Path(settings.HARNESS_STORE_PATH or Path("data") / "harness" / "personas.json")


def default_history_path(store_path: Path | None = None) -> Path:
    if settings.HARNESS_HISTORY_PATH:
        return Path(settings.HARNESS_HISTORY_PATH)
    return (store_path or default_store_path()).parent / "history.jsonl"


def resolve_hmac_key(key: str | None = None) -> str:
    """The HMAC key comes from the environment/secret manager, never a file."""
    return settings.HARNESS_HMAC_KEY if key is None else key


def _warn_unsigned() -> None:
    global _WARNED_UNSIGNED
    if not _WARNED_UNSIGNED:
        _WARNED_UNSIGNED = True
        logger.warning(
            "HARNESS_HMAC_KEY is not set; the persona store and history are unsigned"
        )


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _mac(body: dict, key: str) -> str:
    return hmac_lib.new(key.encode("utf-8"), _canonical(body), hashlib.sha256).hexdigest()


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()


def task_hash(task: str, key: str | None = None) -> str:
    """A keyed hash: the task text is never stored, and a plain hash is avoided."""
    key = resolve_hmac_key(key)
    digest = hmac_lib.new(key.encode("utf-8"), (task or "").encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


def new_store() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "champion": None,
        "champion_history": [],
        "personas": [],
    }


def _quarantine(path: Path, reason: str) -> Path:
    bad = path.with_name(path.name + ".bad")
    try:
        os.replace(path, bad)
    except OSError:
        try:
            bad.write_bytes(path.read_bytes())
            path.unlink()
        except OSError:
            logger.error("Could not quarantine %s: %s", path, reason)
    logger.error("Quarantined persona store %s as %s (%s)", path, bad, reason)
    return bad


def _verify_mac(document: dict, key: str) -> bool:
    mac = document.get("hmac") or {}
    expected = mac.get("mac")
    if not expected:
        return False
    body = {name: value for name, value in document.items() if name != "hmac"}
    return hmac_lib.compare_digest(expected, _mac(body, key))


def load_store(path: Path | str | None = None, key: str | None = None) -> dict:
    """Load the library, quarantining a corrupt or untrusted file.

    Never raises for a missing or corrupt store: the seeds are the fallback.
    A store with a newer schema is rejected instead of guessed at.
    """
    path = Path(path) if path is not None else default_store_path()
    key = resolve_hmac_key(key)
    if not path.exists():
        store = new_store()
        ensure_seed(store)
        return store

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _quarantine(path, f"unreadable JSON: {exc}")
        store = new_store()
        ensure_seed(store)
        return store

    if not isinstance(document, dict):
        _quarantine(path, "store is not a JSON object")
        store = new_store()
        ensure_seed(store)
        return store

    version = document.get("schema_version")
    if isinstance(version, int) and version > SCHEMA_VERSION:
        raise StoreVersionError(
            f"The persona store uses schema_version {version}; this harness supports "
            f"{SCHEMA_VERSION}. Upgrade the harness before using it."
        )

    if document.get("hmac"):
        if not key:
            raise StoreError(
                "The persona store is HMAC-authenticated but HARNESS_HMAC_KEY is not set"
            )
        if not _verify_mac(document, key):
            _quarantine(path, "HMAC verification failed")
            store = new_store()
            ensure_seed(store)
            return store
    elif key:
        logger.warning(
            "The persona store at %s is unsigned although HARNESS_HMAC_KEY is set; "
            "it will be signed on the next save",
            path,
        )

    if not isinstance(document.get("personas"), list):
        _quarantine(path, "personas is not a list")
        store = new_store()
        ensure_seed(store)
        return store

    document.setdefault("schema_version", SCHEMA_VERSION)
    document.setdefault("updated_at", utc_now())
    document.setdefault("champion", None)
    document.setdefault("champion_history", [])
    ensure_seed(document)
    return document


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def save_store(store: dict, path: Path | str | None = None, key: str | None = None) -> Path:
    path = Path(path) if path is not None else default_store_path()
    key = resolve_hmac_key(key)
    store["schema_version"] = SCHEMA_VERSION
    store["updated_at"] = utc_now()
    body = {name: value for name, value in store.items() if name != "hmac"}
    if key:
        body["hmac"] = {"algorithm": "HMAC-SHA256", "key_id": "env", "mac": _mac(body, key)}
    else:
        _warn_unsigned()
    _write_atomic(path, json.dumps(body, indent=2, ensure_ascii=False) + "\n")
    return path


# --- run history --------------------------------------------------------------


def read_history(
    path: Path | str | None = None, limit: int | None = None, key: str | None = None
) -> list[dict]:
    path = Path(path) if path is not None else default_history_path()
    key = resolve_hmac_key(key)
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            logger.warning("Skipping a corrupt history line in %s", path)
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("hmac") and key:
            mac = entry.get("hmac")
            body = {name: value for name, value in entry.items() if name != "hmac"}
            if not hmac_lib.compare_digest(mac, _mac(body, key)):
                logger.warning("Skipping a history line with a bad HMAC in %s", path)
                continue
        entries.append(entry)
    if limit is not None:
        if limit <= 0:
            return []
        entries = entries[-limit:]
    return entries


def append_history(
    entry: dict,
    path: Path | str | None = None,
    key: str | None = None,
) -> Path:
    path = Path(path) if path is not None else default_history_path()
    key = resolve_hmac_key(key)
    run_id = entry.get("run_id")
    if not run_id:
        raise StoreError("history entries must have a run_id")
    existing = read_history(path, key=key)
    if any(item.get("run_id") == run_id for item in existing):
        raise DuplicateRunId(f"Run {run_id} is already in the history")

    entry = dict(entry)
    entry["schema_version"] = HISTORY_SCHEMA_VERSION
    if not entry.get("timestamp"):
        entry["timestamp"] = utc_now()
    body = {name: value for name, value in entry.items() if name != "hmac"}
    if key:
        body["hmac"] = _mac(body, key)
    else:
        _warn_unsigned()

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return path


# --- champion -----------------------------------------------------------------


def champion_persona(store: dict) -> dict | None:
    champion = store.get("champion") or {}
    champion_id = champion.get("persona_id")
    if not champion_id:
        return None
    for persona in store.get("personas", []):
        if persona.get("id") == champion_id:
            return persona
    return None


def load_champion(path: Path | str | None = None, key: str | None = None) -> dict | None:
    """Load the champion for the team pipeline; fail closed when it is invalid."""
    store = load_store(path, key=key)
    persona = champion_persona(store)
    if persona is None:
        return None
    valid, reason = validate_persona(persona)
    if not valid:
        raise PersonaSecurityError(
            f"The harness champion {persona.get('id')} is invalid: {reason}. "
            "The audit is blocked; promote a valid persona or roll back."
        )
    if persona.get("status") != "active":
        raise PersonaSecurityError(
            f"The harness champion {persona.get('id')} is not active. "
            "The audit is blocked; promote a valid persona or roll back."
        )
    expected = (store.get("champion") or {}).get("prompt_sha256")
    actual = prompt_sha256(persona.get("prompt", ""))
    if expected and not hmac_lib.compare_digest(expected, actual):
        raise PersonaSecurityError(
            f"The harness champion {persona.get('id')} does not match its recorded "
            "prompt hash. The audit is blocked; promote a valid persona or roll back."
        )
    logger.info(
        "Team audit uses harness champion %s (prompt sha256 %s)",
        persona.get("id"),
        actual,
    )
    return persona

"""Seed personas and the identifiers used by the Harness2 persona library."""

import hashlib
from datetime import UTC, datetime

FOCUS_AREAS = (
    "authentication",
    "authorization",
    "secrets",
    "input_validation",
    "tenant_isolation",
    "data_protection",
    "rate_limiting",
    "business_logic",
    "cloud_iam",
    "cryptography",
    "logging",
    "supply_chain",
)

STRICTNESS = ("standard", "strict", "red_team")

_FOCUS_KEYWORDS = {
    "authentication": ("authentication", "auth", "login", "session", "token"),
    "authorization": ("authorization", "privilege", "access control", "tenant"),
    "secrets": ("secret", "credential", "key management", "api key"),
    "input_validation": ("input", "validation", "injection", "deserial"),
    "tenant_isolation": ("tenant", "isolation", "multi-tenant"),
    "data_protection": ("privacy", "pii", "data protection", "retention", "encryption"),
    "rate_limiting": ("rate limit", "abuse", "replay", "idempot", "exhaustion"),
    "business_logic": ("business logic", "fraud", "workflow", "abuse"),
    "cloud_iam": ("cloud", "iam", "network", "blast radius", "segmentation"),
    "cryptography": ("crypto", "tls", "encryption", "hashing"),
    "logging": ("logging", "audit log", "monitoring", "observability"),
    "supply_chain": ("dependency", "supply chain", "package", "vendor"),
}

FALLBACK_CANDIDATES = [
    "You are a strict compliance auditor. Review the design against OWASP API "
    "Security Top 10 and PCI-DSS; cite the exact control each finding violates.",
    "You are a red-team adversary. Hunt for business-logic abuse, broken "
    "authorization chains and realistic attack paths an attacker would use.",
    "You are a cloud security architect. Scrutinize IAM boundaries, network "
    "segmentation, multi-tenant isolation, secrets management and blast radius.",
    "You are a privacy and data-protection reviewer. Focus on PII flows, data "
    "minimization, retention, encryption and regulatory exposure.",
    "You are an abuse-prevention engineer. Prioritize rate limiting, idempotency, "
    "replay protection, fraud paths and resource-exhaustion vectors.",
]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def persona_id(prompt: str) -> str:
    """Stable id, so re-seeding the same persona does not duplicate it."""
    digest = hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()
    return f"p_{digest[:12]}"


def emphasis_for_prompt(prompt: str) -> dict:
    """A constrained EmphasisSpec, derived from the fixed focus-area vocabulary."""
    lowered = (prompt or "").lower()
    focus = [
        area for area in FOCUS_AREAS if any(word in lowered for word in _FOCUS_KEYWORDS.get(area, ()))
    ]
    if "red team" in lowered or "red-team" in lowered or "adversar" in lowered:
        strictness = "red_team"
    elif "strict" in lowered or "compliance" in lowered:
        strictness = "strict"
    else:
        strictness = "standard"
    return {"focus_areas": focus, "strictness": strictness}


def valid_emphasis(emphasis: object) -> bool:
    if not isinstance(emphasis, dict):
        return False
    focus = emphasis.get("focus_areas")
    if not isinstance(focus, list) or any(area not in FOCUS_AREAS for area in focus):
        return False
    return emphasis.get("strictness") in STRICTNESS


def new_persona(
    prompt: str,
    origin: str = "seed",
    parent_id: str | None = None,
    created: str | None = None,
) -> dict:
    prompt = prompt.strip()
    return {
        "id": persona_id(prompt),
        "prompt": prompt,
        "origin": origin,
        "parent_id": parent_id,
        "created": created or utc_now(),
        "status": "active",
        "emphasis": emphasis_for_prompt(prompt),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "stats": {
            "runs": 0,
            "wins": 0,
            "approvals": 0,
            "failures": 0,
            "mean_revisions": None,
            "mean_weighted": None,
            "weakness_categories": [],
            "last_run": None,
        },
    }


def seed_personas() -> list[dict]:
    return [new_persona(prompt, origin="seed") for prompt in FALLBACK_CANDIDATES]

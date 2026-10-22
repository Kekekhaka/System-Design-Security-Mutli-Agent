"""A non-LLM check for designs that are obviously incomplete.

The baseline auditor is a model, so it can miss an explicit statement that
authentication is disabled or a security TODO. This guard is deliberately
narrow: it only fires on explicit text, never on a judgement call, so it cannot
inflate findings or block a sound design.
"""

import re

_EXPLICIT = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bauthentication\s+is\s+disabled\b",
        r"\bauthorization\s+is\s+disabled\b",
        r"\bno\s+authentication\s+(?:checks?|controls?|required|layer|enforced)\b",
        r"\bno\s+authorization\s+(?:checks?|controls?|enforced|layer)\b",
        r"\bskip(?:ping)?\s+(?:the\s+)?(?:auth|authorization|authentication)\b",
        r"\bany\s+(?:authenticated\s+)?user\s+can\s+(?:read|write|delete)\s+any\b",
        r"\btrusts?\s+the\s+tenant\s+id\s+from\s+the\s+request\b",
    )
]

_TODO = re.compile(
    r"(?:\b(?:auth\w*|security|encrypt\w*|authoriz\w*)\b[^\n.]{0,80}\bTODO\b"
    r"|\bTODO\b[^\n.]{0,80}\b(?:auth\w*|security|encrypt\w*|authoriz\w*)\b)",
    re.IGNORECASE,
)


def critical_rule_guard(architecture: str | None, api_spec: str | None) -> str | None:
    """Return a reason when an obvious security control is explicitly missing."""
    text = f"{architecture or ''}\n{api_spec or ''}"
    for pattern in _EXPLICIT:
        match = pattern.search(text)
        if match:
            return f"the design explicitly states an insecure control: {match.group(0)!r}"
    match = _TODO.search(text)
    if match:
        return f"an unfinished security control is marked TODO: {match.group(0)!r}"
    return None

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
        r"\bno\s+authentication\s+(?:checks?|controls?|layer|enforced)\b",
        r"\bno\s+authorization\s+(?:checks?|controls?|enforced|layer)\b",
        r"\bskip(?:ping)?\s+(?:the\s+)?(?:auth|authorization|authentication)\b",
        r"\bany\s+(?:authenticated\s+)?user\s+can\s+(?:read|write|delete)\s+any\b",
        r"\btrusts?\s+the\s+tenant\s+id\s+from\s+the\s+request\b",
    )
]

# "No authentication required" is normal for a health check, a login route or
# a signed webhook. It is exempt only when such a route is what the line is
# about: the line opens with its path, as a route list or table row does
# ("GET /health - ...", "| POST /webhooks/stripe | ..."), and the phrase comes
# after it. A public path mentioned later in the line, or a bare word such as
# "public" or "token", does not exempt a gap on another route.
_NO_AUTH_REQUIRED = re.compile(r"\bno\s+authentication\s+required\b", re.IGNORECASE)
_METHOD = r"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)"
# A token endpoint counts only as /auth/token or /oauth(2)/token, where the
# client authenticates with its own credentials.
_PUBLIC_PATH = (
    r"/(?:api/)?(?:v\d+/)?"
    r"(?:(?:auth/)?(?:health(?:z|check)?|livez?|readyz?|liveness|readiness|metrics|login|signin|"
    r"signup|sign-up|register|webhooks?|docs|redoc|openapi(?:\.json)?|\.well-known)"
    r"|(?:auth|oauth2?)/token)(?![\w-])[^\s|,:;`\"')]*"
)
_PUBLIC_ROUTE_LEAD = re.compile(
    r"^\s*(?:[-*+|>]\s*|\d+[.)]\s*)?[`\"']?"
    rf"(?:{_METHOD}(?:\s*[,/]\s*{_METHOD})*\s+)?[`\"']?{_PUBLIC_PATH}",
    re.IGNORECASE,
)
# Between the public route and the phrase only separators, further public
# paths and a few joining words may appear; anything else is another subject.
_PUBLIC_PATH_ANYWHERE = re.compile(rf"(?<![^\s`\"'(]){_PUBLIC_PATH}", re.IGNORECASE)
_JOINING_WORDS = re.compile(rf"\b(?:and|or|none|public|{_METHOD})\b", re.IGNORECASE)

# A TODO marker, not the word inside "TODO list".
_TODO = re.compile(
    r"(?:\b(?:auth\w*|security|encrypt\w*|authoriz\w*)\b[^\n.]{0,80}\bTODO\b(?!\s+lists?\b)"
    r"|\bTODO\b(?!\s+lists?\b)[^\n.]{0,80}\b(?:auth\w*|security|encrypt\w*|authoriz\w*)\b)",
    re.IGNORECASE,
)


def critical_rule_guard(architecture: str | None, api_spec: str | None) -> str | None:
    """Return a reason when an obvious security control is explicitly missing."""
    text = f"{architecture or ''}\n{api_spec or ''}"
    for pattern in _EXPLICIT:
        match = pattern.search(text)
        if match:
            return f"the design explicitly states an insecure control: {match.group(0)!r}"
    for line in text.splitlines():
        match = _NO_AUTH_REQUIRED.search(line)
        if not match:
            continue
        lead = _PUBLIC_ROUTE_LEAD.match(line)
        between = line[lead.end():match.start()] if lead else ""
        between = _JOINING_WORDS.sub("", _PUBLIC_PATH_ANYWHERE.sub("", between))
        if not lead or match.start() < lead.end() or re.search(r"[\w/]", between):
            return f"the design explicitly states an insecure control: {match.group(0)!r}"
    match = _TODO.search(text)
    if match:
        return f"an unfinished security control is marked TODO: {match.group(0)!r}"
    return None

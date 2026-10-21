"""Load the ProjectCosmos role prompts shipped with the package."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

TEAM_DIR = Path(__file__).resolve().parent
ROLES_DIR = TEAM_DIR / "roles"
TEAM_RULES_PATH = TEAM_DIR / "TEAM_RULES.md"

REQUIRED_KEYS = ("name", "description", "stage", "artifact", "used_when")
REQUIRED_SECTIONS = ("## Role", "## Exit gate")


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    stage: int
    artifact: str
    used_when: str
    body: str


def _parse_front_matter(text: str, source: Path) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{source.name}: missing front matter")
    try:
        end = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration:
        raise ValueError(f"{source.name}: unterminated front matter") from None

    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"{source.name}: invalid front matter line: {line!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key.strip()] = value

    body = "\n".join(lines[end + 1 :]).strip()
    return fields, body


def _load(directory: Path) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    for path in sorted(directory.glob("*.md")):
        fields, body = _parse_front_matter(path.read_text(encoding="utf-8"), path)
        for key in REQUIRED_KEYS:
            if not fields.get(key):
                raise ValueError(f"{path.name}: missing required key {key!r}")
        try:
            stage = int(fields["stage"])
        except ValueError:
            raise ValueError(
                f"{path.name}: stage must be an integer, got {fields['stage']!r}"
            ) from None
        missing = [section for section in REQUIRED_SECTIONS if section not in body]
        if missing:
            raise ValueError(f"{path.name}: body is missing {', '.join(missing)}")
        name = fields["name"]
        if name in roles:
            raise ValueError(f"{path.name}: duplicate role name {name!r}")
        roles[name] = Role(
            name=name,
            description=fields["description"],
            stage=stage,
            artifact=fields["artifact"],
            used_when=fields["used_when"],
            body=body,
        )
    return roles


@lru_cache(maxsize=1)
def _default_roles() -> dict[str, Role]:
    return _load(ROLES_DIR)


def load_roles(directory: Path | None = None) -> dict[str, Role]:
    """Load every role file, keyed by name. The default load is cached."""
    if directory is None:
        return _default_roles()
    return _load(Path(directory))


@lru_cache(maxsize=1)
def team_rules() -> str:
    return TEAM_RULES_PATH.read_text(encoding="utf-8").strip()


def role_prompt(name: str) -> str:
    """The team rules followed by the named role's body."""
    roles = load_roles()
    try:
        role = roles[name]
    except KeyError:
        known = ", ".join(sorted(roles))
        raise KeyError(f"unknown role {name!r}; known roles: {known}") from None
    return f"{team_rules()}\n\n{role.body}"

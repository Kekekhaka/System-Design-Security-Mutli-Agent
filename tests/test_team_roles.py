from pathlib import Path

import pytest

from src.agents.prompts import DESIGNER_SYSTEM_PROMPT
from src.agents.security.prompts import SECURITY_SYSTEM_PROMPT
from src.agents.team.roles import Role, load_roles, role_prompt, team_rules

TEAM_DIR = Path(__file__).resolve().parents[1] / "src" / "agents" / "team"
ROLE_NAMES = {
    "team-lead",
    "ideologist",
    "business-analyst",
    "architect",
    "security-engineer",
    "devops-engineer",
    "lead-engineer",
    "backend-engineer",
    "frontend-engineer",
    "reviewer",
    "qa-analyst",
}
FORBIDDEN = (
    "subagent_type",
    ".claude/",
    "tools:",
    "model: sonnet",
    "model: haiku",
    "WebSearch",
    "tool calls",
)


def test_all_roles_load() -> None:
    roles = load_roles()

    assert set(roles) == ROLE_NAMES
    assert all(isinstance(role, Role) for role in roles.values())
    assert all(isinstance(role.stage, int) for role in roles.values())


def test_team_files_have_no_claude_code_references() -> None:
    paths = [TEAM_DIR / "TEAM_RULES.md", *sorted((TEAM_DIR / "roles").glob("*.md"))]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN:
            assert forbidden not in text, f"{path.name} contains {forbidden!r}"


def test_every_role_has_role_and_exit_gate() -> None:
    for name, role in load_roles().items():
        assert "## Role" in role.body, name
        assert "## Exit gate" in role.body, name


def test_role_prompt_combines_rules_and_body() -> None:
    prompt = role_prompt("architect")
    role = load_roles()["architect"]

    assert prompt.startswith(team_rules())
    assert prompt.endswith(role.body)
    assert role.body in prompt


def test_role_prompt_unknown_name_lists_known_names() -> None:
    with pytest.raises(KeyError) as excinfo:
        role_prompt("nope")

    message = str(excinfo.value)
    for name in ROLE_NAMES:
        assert name in message


def _write_role(
    directory: Path,
    filename: str,
    *,
    name: str = "architect",
    stage: str = "3",
    body: str = "## Role\nDesign.\n\n## Exit gate\nDone.\n",
) -> None:
    (directory / filename).write_text(
        "---\n"
        f"name: {name}\n"
        'description: "A role."\n'
        f"stage: {stage}\n"
        "artifact: DESIGN_SPEC\n"
        'used_when: "When needed."\n'
        "---\n"
        f"{body}",
        encoding="utf-8",
    )


def test_load_roles_rejects_missing_key(tmp_path: Path) -> None:
    (tmp_path / "broken.md").write_text(
        "---\n"
        "name: architect\n"
        'description: "A role."\n'
        "artifact: DESIGN_SPEC\n"
        'used_when: "When needed."\n'
        "---\n"
        "## Role\nDesign.\n\n## Exit gate\nDone.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="broken.md"):
        load_roles(tmp_path)


def test_load_roles_rejects_non_integer_stage(tmp_path: Path) -> None:
    _write_role(tmp_path, "bad-stage.md", stage="three")

    with pytest.raises(ValueError, match="bad-stage.md"):
        load_roles(tmp_path)


def test_load_roles_rejects_duplicate_name(tmp_path: Path) -> None:
    _write_role(tmp_path, "one.md")
    _write_role(tmp_path, "two.md")

    with pytest.raises(ValueError, match="duplicate"):
        load_roles(tmp_path)


def test_load_roles_rejects_missing_exit_gate(tmp_path: Path) -> None:
    _write_role(tmp_path, "no-gate.md", body="## Role\nDesign.\n")

    with pytest.raises(ValueError, match="Exit gate"):
        load_roles(tmp_path)


def test_designer_prompt_has_architect_standards() -> None:
    prompt = DESIGNER_SYSTEM_PROMPT

    assert "Architect role (team standards):" in prompt
    assert "Cost budget" in prompt
    assert "===ARCHITECTURE===" in prompt
    assert "===API SPEC===" in prompt
    assert prompt.index("Architect role (team standards):") < prompt.index(
        "Output format (strict"
    )


def test_security_prompt_has_security_engineer_standards() -> None:
    prompt = SECURITY_SYSTEM_PROMPT
    protocol = prompt[prompt.index("Output protocol (strict):") :]

    assert "Security engineer role (team standards):" in prompt
    assert "OWASP LLM Top 10" in prompt
    assert prompt.index("Security engineer role (team standards):") < prompt.index(
        "Output protocol (strict):"
    )
    assert protocol.startswith("Output protocol (strict):")
    assert 'start your reply with exactly "APPROVED"' in protocol
    assert '"CHANGES REQUESTED"' in protocol

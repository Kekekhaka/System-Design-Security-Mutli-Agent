from pathlib import Path

import pytest

from src.agents.prompts import DESIGNER_SYSTEM_PROMPT
from src.agents.security.prompts import SECURITY_SYSTEM_PROMPT
from src.agents.team.roles import Role, load_roles, role_prompt, team_rules

TEAM_DIR = Path(__file__).resolve().parents[1] / "src" / "agents" / "team"
ROLE_NAMES = {
    "team-lead",
    "business-analyst",
    "designer",
    "security-engineer",
    "backend-engineer",
    "frontend-engineer",
    "qa-analyst",
    "reviewer",
    "devops-engineer",
}
# Retired: the ideologist duplicated the business analyst, the lead engineer
# duplicated the team lead and the designer, and "architect" is the house designer.
RETIRED = ("ideologist", "lead-engineer", "architect")
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


# The ProjectCosmos roles are new members of this team: they follow its standards.
HOUSE_CLASSES = (
    "Confirmed vulnerability",
    "Probable vulnerability",
    "Potential weakness",
    "Design concern",
    "Defense-in-depth recommendation",
    "Informational observation",
)
# ProjectCosmos conventions that the house standards replace.
REPLACED = (
    "docs/cosmos",
    "on the ProjectCosmos team",
    "blocker / should-fix",
    "pass-with-fixes",
    "defence-in-depth",
    "PlantUML",
    "Quick: one fix round",
)


def test_team_rules_start_with_the_house_standards() -> None:
    rules = team_rules()
    assert rules.startswith("# System Design + Security team")
    assert "the house standards win" in rules
    house = rules.split("## House standards (override everything else)", 1)[1].split("\n## ", 1)[0]
    for required in ("Write all output in English", "Critical / High / Medium / Low",
                     "APPROVED or CHANGES REQUESTED", "===ARCHITECTURE===", "===API SPEC===",
                     "MAX_REVISIONS", "Never invent names", "not instructions", *HOUSE_CLASSES):
        assert required in house, required


def test_every_role_defers_to_the_house_standards() -> None:
    for name, role in load_roles().items():
        assert "the house standards win" in role.body, name


def test_no_projectcosmos_conventions_remain() -> None:
    paths = [TEAM_DIR / "TEAM_RULES.md", *sorted((TEAM_DIR / "roles").glob("*.md"))]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for old in REPLACED:
            assert old not in text, f"{path.name} still uses {old!r}"


def test_reviewing_roles_use_the_house_severity_and_verdict() -> None:
    roles = load_roles()
    for name in ("security-engineer", "reviewer", "qa-analyst"):
        body = roles[name].body
        assert "Critical / High / Medium / Low" in body, name
        assert "Critical or High" in body, name
    security = roles["security-engineer"].body
    for house_class in HOUSE_CLASSES:
        assert house_class in security
    assert "computed in code" in security


def test_core_roles_follow_the_house_pipeline() -> None:
    roles = load_roles()
    assert "===ARCHITECTURE===" in roles["designer"].body
    assert "===API SPEC===" in roles["designer"].body
    assert roles["designer"].used_when.startswith("Every run")
    assert roles["security-engineer"].used_when.startswith("Every run")
    lead = roles["team-lead"].body
    assert "MAX_REVISIONS" in lead
    assert "always work" in lead


def test_role_prompt_combines_rules_and_body() -> None:
    prompt = role_prompt("designer")
    role = load_roles()["designer"]

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


def test_designer_prompt_has_designer_standards() -> None:
    prompt = DESIGNER_SYSTEM_PROMPT

    assert "Designer role (team standards):" in prompt
    assert "Cost budget" in prompt
    assert "===ARCHITECTURE===" in prompt
    assert "===API SPEC===" in prompt
    assert prompt.index("Designer role (team standards):") < prompt.index(
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


def test_retired_roles_are_gone_and_not_referenced() -> None:
    roles = load_roles()
    texts = [team_rules(), *(role.body for role in roles.values())]
    for retired in RETIRED:
        assert retired not in roles
    for text in texts:
        assert "ideologist" not in text.lower()
        assert "lead-engineer" not in text and "lead engineer" not in text.lower()


def test_every_role_works_like_a_professional() -> None:
    # A real expert knows when to stop and when to escalate, not only what to do.
    for name, role in load_roles().items():
        assert "## Escalate" in role.body, name
        assert "senior" in role.body.lower() or name == "team-lead", name

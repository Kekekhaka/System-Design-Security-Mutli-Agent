"""The team pipeline's design stages (design-only runs)."""

import importlib.util
import json
from pathlib import Path

import pytest

from src.agents.team.graph import (
    PIPELINE_NOTE,
    RUN_CONFIG,
    build_team_graph,
    initial_team_state,
    next_role,
)
from src.agents.team.plan import parse_plan
from src.agents.team.roles import load_roles
from src.core.config import settings
from src.core.llm import ModelOutputError
from tests.fakes import ScriptedLLM

DESIGN_V1 = "===ARCHITECTURE===\nArchitecture v1: raw card numbers reach the backend.\n===API SPEC===\nPOST /pay {card_number}"
DESIGN_V2 = "===ARCHITECTURE===\nArchitecture v2: tokens only.\n===API SPEC===\nPOST /pay {payment_token}"
REJECT_ROUND = ["Scope notes.", "Draft: raw card data.", "CHANGES REQUESTED\nRaw card numbers reach the backend."]
APPROVE_ROUND = ["Scope notes.", "Draft: tokens only.", "APPROVED\nOnly tokens reach the backend."]
PACK = "FR-01 Pay with a card.\nAC-01 Given a card, When paying, Then only a token reaches the backend."
SIGN_OFF = "Built the payment design; all gates met."


def plan_json(**overrides) -> str:
    plan = {
        "mode": "quick",
        "staff": ["business-analyst", "designer", "security-engineer"],
        "skip_reasons": {},
        "questions_for_operator": [],
        "decisions": ["Quick mode: one clear API."],
    }
    plan.update(overrides)
    return json.dumps(plan)


def run(responses, brief="Design a card payment API.", answers=None):
    llm = ScriptedLLM(responses)
    graph = build_team_graph(llm=llm)
    return graph.invoke(initial_team_state(brief, operator_answers=answers), config=RUN_CONFIG), llm


def system_texts(llm):
    return [call[0].content for call in llm.calls]


def test_quick_run_staffs_ba_then_designer_then_security_then_sign_off():
    result, llm = run([plan_json(), PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF])

    assert result["status"] == "approved"
    roles = [h["role"] for h in result["handoffs"]]
    assert roles == ["team-lead", "business-analyst", "designer", "security-engineer", "team-lead"]
    systems = system_texts(llm)
    assert load_roles()["team-lead"].body in systems[0]
    assert load_roles()["business-analyst"].body in systems[1]
    assert all(PIPELINE_NOTE in text for i, text in enumerate(systems) if i in (0, 1, 6))
    assert result["model_calls"] == 1 + 1 + 1 + 3 + 1
    assert result["opencode_runs"] == 0


def test_requirements_pack_reaches_the_designer():
    result, llm = run([plan_json(), PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF])

    designer_prompt = llm.calls[2][1].content
    assert "FR-01 Pay with a card." in designer_prompt
    assert "Trace design choices to these IDs" in designer_prompt
    assert "FR-01" in result["requirements"]


def test_skipped_analyst_goes_straight_to_the_designer():
    plan = plan_json(staff=["designer", "security-engineer"], skip_reasons={"business-analyst": "Tiny change."})
    result, _ = run([plan, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF])

    assert result["requirements_pack"] is None
    assert "Skipped business-analyst: Tiny change." in result["decisions"]
    assert result["status"] == "approved"


def test_questions_stop_the_run_for_the_operator():
    result, llm = run([plan_json(questions_for_operator=["One clinic or many?"])])

    assert result["status"] == "needs_operator"
    assert result["plan"]["questions"] == ["One clinic or many?"]
    assert len(llm.calls) == 1
    assert result["architecture"] is None


def test_with_answers_the_lead_proceeds_and_does_not_escalate_again():
    responses = [plan_json(questions_for_operator=["One clinic or many?"]), PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF]
    result, llm = run(responses, answers="One clinic.")

    assert result["status"] == "approved"
    assert "One clinic." in llm.calls[0][1].content


def test_escalates_at_the_house_revision_limit(monkeypatch):
    monkeypatch.setattr(settings, "MAX_REVISIONS", 2)
    responses = [plan_json(), PACK, DESIGN_V1, *REJECT_ROUND, DESIGN_V2, *REJECT_ROUND, "Sign-off text claiming APPROVED."]
    result, _ = run(responses)

    assert result["revision_count"] == 2
    assert result["status"] == "escalated"
    # The verdict line is computed in code, in the house vocabulary, whatever the model writes.
    assert result["sign_off"].startswith("Verdict: CHANGES REQUESTED")
    assert "house revision limit of 2" in result["sign_off"].splitlines()[0]
    assert any("sent back to the designer" in d for d in result["decisions"])
    assert "Escalated to the operator: CHANGES REQUESTED." in result["decisions"]


def test_mode_never_changes_the_house_revision_limit(monkeypatch):
    # ProjectCosmos capped fix rounds by mode (quick 1, full 2); the house limit wins.
    monkeypatch.setattr(settings, "MAX_REVISIONS", 3)
    responses = [plan_json(mode="small"), PACK, DESIGN_V1, *REJECT_ROUND, DESIGN_V2, *REJECT_ROUND, DESIGN_V2, *APPROVE_ROUND, SIGN_OFF]
    result, _ = run(responses)

    assert result["revision_count"] == 3
    assert result["status"] == "approved"
    assert result["sign_off"].startswith("Verdict: APPROVED")


def test_every_role_is_told_to_follow_the_house_standards_in_english():
    _, llm = run([plan_json(), PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF])

    for index in (0, 1, 6):  # team lead plan, business analyst, team lead sign-off
        system = llm.calls[index][0].content
        assert "House standards (override everything else)" in system
        assert "follow the house standards in the team rules first" in system
        assert "Write all output in English." in system


def test_plan_parsing_forces_core_roles_and_drops_unknown_ones():
    plan = parse_plan(plan_json(staff=["business-analyst", "architect", "ideologist"], mode="huge"))

    assert plan.staff == ("business-analyst", "designer", "security-engineer")
    assert plan.mode == "quick"
    assert any("architect" in d and "ideologist" in d for d in plan.decisions)  # retired roles
    assert any("Always staffed" in d for d in plan.decisions)


def test_design_only_run_drops_build_roles():
    plan = parse_plan(plan_json(staff=["designer", "security-engineer", "backend-engineer", "reviewer"]))

    assert plan.staff == ("designer", "security-engineer")
    assert any(d.startswith("Design-only run: dropped backend-engineer, reviewer") for d in plan.decisions)


def test_unreadable_plan_falls_back_to_the_default_quick_plan():
    for text in ("no json here", "{not json}", "[1, 2]"):
        plan = parse_plan(text)
        assert plan.mode == "quick"
        assert plan.staff == ("business-analyst", "designer", "security-engineer")
        assert plan.decisions and "default quick plan" in plan.decisions[0]


def test_next_role_is_pure_routing():
    assert next_role({"status": "needs_operator"}) == "FINISH"
    assert next_role({"status": "running"}) == "team_lead_plan"
    state = {"status": "running", "plan": {"staff": ["designer", "security-engineer"], "mode": "quick"}}
    assert next_role(state) == "designer"
    assert next_role({**state, "architecture": "a"}) == "security_engineer"
    approved = {**state, "architecture": "a", "security_findings": "APPROVED\nok"}
    assert next_role(approved) == "team_lead_sign_off"  # design-only: no build stages


def load_run_team():
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_team.py"
    spec = importlib.util.spec_from_file_location("run_team", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_write_run_writes_artifacts_and_log(tmp_path: Path):
    result, _ = run([plan_json(), PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF])

    written = {path.name for path in load_run_team().write_run(result, tmp_path / "run")}

    assert {"REQUIREMENTS_PACK.md", "architecture.md", "api-spec.md", "security-report.md",
            "SIGN_OFF.md", "team-log.json"} <= written
    assert not written & {"TEST_REPORT.md", "REVIEW_REPORT.md", "code-security-report.md", "changes.diff"}
    log = json.loads((tmp_path / "run" / "team-log.json").read_text(encoding="utf-8"))
    assert log["status"] == "approved" and log["model_calls"] == 7


def test_progress_is_printed_per_step_and_matches_a_plain_run(tmp_path: Path):
    responses = [plan_json(), PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF]
    expected, _ = run(responses)
    lines = []

    graph = build_team_graph(llm=ScriptedLLM(responses))
    state = initial_team_state("Design a card payment API.")
    result = load_run_team().run_with_progress(graph, state, tmp_path / "out", printer=lambda line, **_: lines.append(line))

    steps = [line.split("] ", 1)[1].split(":", 1)[0] for line in lines]
    assert steps == ["team_lead_plan", "business_analyst", "designer", "security_engineer", "team_lead_sign_off"]
    for key in ("status", "sign_off", "architecture", "requirements_pack", "revision_count", "model_calls"):
        assert result[key] == expected[key], key
    assert result["handoffs"] == expected["handoffs"]
    assert result["decisions"] == expected["decisions"]


def test_a_crash_keeps_everything_finished_so_far(tmp_path: Path):
    # The sign-off returns empty output every time: invoke_checked gives up and raises.
    responses = [plan_json(), PACK, DESIGN_V1, *APPROVE_ROUND, ""]
    graph = build_team_graph(llm=ScriptedLLM(responses))
    out = tmp_path / "out"

    with pytest.raises(ModelOutputError, match="sign-off"):
        load_run_team().run_with_progress(graph, initial_team_state("Design it."), out, printer=lambda *a, **k: None)

    for name in ("REQUIREMENTS_PACK.md", "architecture.md", "api-spec.md", "security-report.md", "team-log.json"):
        assert (out / name).exists(), name
    assert not (out / "SIGN_OFF.md").exists()

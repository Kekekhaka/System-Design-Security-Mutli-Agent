import importlib.util
import json
from pathlib import Path

from src.agents.team.graph import (
    PIPELINE_NOTE,
    build_team_graph,
    initial_team_state,
    next_role,
)
from src.agents.team.plan import parse_plan
from src.agents.team.roles import load_roles
from tests.fakes import ScriptedLLM

DESIGN_V1 = "===ARCHITECTURE===\nArchitecture v1: raw card numbers reach the backend.\n===API SPEC===\nPOST /pay {card_number}"
DESIGN_V2 = "===ARCHITECTURE===\nArchitecture v2: tokens only.\n===API SPEC===\nPOST /pay {payment_token}"
REJECT_ROUND = ["Scope notes.", "Draft: raw card data.", "CHANGES REQUESTED\nRaw card numbers reach the backend."]
APPROVE_ROUND = ["Scope notes.", "Draft: tokens only.", "APPROVED\nOnly tokens reach the backend."]
PACK = "FR-01 Pay with a card.\nAC-01 Given a card, When paying, Then only a token reaches the backend."
IDEA = "Concept: card payments for a small shop."
SIGN_OFF = "Built the payment design; all gates met."


def plan_json(**overrides) -> str:
    plan = {
        "mode": "quick",
        "staff": ["business-analyst", "architect", "security-engineer"],
        "skip_reasons": {"ideologist": "The brief is clear."},
        "questions_for_operator": [],
        "decisions": ["Quick mode: one clear API."],
    }
    plan.update(overrides)
    return json.dumps(plan)


def run(responses, brief="Design a card payment API.", answers=None):
    llm = ScriptedLLM(responses)
    graph = build_team_graph(llm=llm)
    return graph.invoke(initial_team_state(brief, operator_answers=answers)), llm


def system_texts(llm):
    return [call[0].content for call in llm.calls]


def test_quick_run_staffs_ba_then_architect_then_security_then_sign_off():
    result, llm = run([plan_json(), PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF])

    assert result["status"] == "approved"
    roles = [h["role"] for h in result["handoffs"]]
    assert roles == ["team-lead", "business-analyst", "architect", "security-engineer", "team-lead"]
    systems = system_texts(llm)
    assert load_roles()["team-lead"].body in systems[0]
    assert load_roles()["business-analyst"].body in systems[1]
    assert all(PIPELINE_NOTE in text for i, text in enumerate(systems) if i in (0, 1, 6))
    assert result["model_calls"] == 1 + 1 + 1 + 3 + 1


def test_requirements_pack_reaches_the_architect():
    result, llm = run([plan_json(), PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF])

    architect_prompt = llm.calls[2][1].content
    assert "FR-01 Pay with a card." in architect_prompt
    assert "Trace design choices to these IDs" in architect_prompt
    assert "FR-01" in result["requirements"]


def test_ideologist_runs_before_the_analyst_when_staffed():
    staff = ["ideologist", "business-analyst", "architect", "security-engineer"]
    result, llm = run([plan_json(staff=staff, skip_reasons={}), IDEA, PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF])

    assert [h["role"] for h in result["handoffs"]][:3] == ["team-lead", "ideologist", "business-analyst"]
    assert IDEA in llm.calls[2][1].content  # the analyst sees the idea brief
    assert IDEA in llm.calls[3][1].content  # and so does the architect


def test_skipped_analyst_goes_straight_to_the_architect():
    plan = plan_json(staff=["architect", "security-engineer"], skip_reasons={"business-analyst": "Tiny change."})
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


def test_quick_mode_escalates_after_one_fix_round():
    responses = [plan_json(), PACK, DESIGN_V1, *REJECT_ROUND, DESIGN_V2, *REJECT_ROUND, "Sign-off text claiming APPROVED."]
    result, _ = run(responses)

    assert result["revision_count"] == 2
    assert result["status"] == "escalated"
    # The verdict line is computed in code, whatever the model writes.
    assert result["sign_off"].startswith("Verdict: ESCALATED TO OPERATOR")
    assert any("sent back to the architect" in d for d in result["decisions"])
    assert any(d.startswith("Escalated: security still requests changes") for d in result["decisions"])


def test_full_mode_allows_two_fix_rounds():
    responses = [plan_json(mode="full"), PACK, DESIGN_V1, *REJECT_ROUND, DESIGN_V2, *REJECT_ROUND, DESIGN_V2, *APPROVE_ROUND, SIGN_OFF]
    result, _ = run(responses)

    assert result["revision_count"] == 3
    assert result["status"] == "approved"
    assert result["sign_off"].startswith("Verdict: APPROVED")


def test_plan_parsing_forces_core_roles_and_drops_unknown_ones():
    plan = parse_plan(plan_json(staff=["business-analyst", "devops-engineer"], mode="huge"))

    assert plan.staff == ("business-analyst", "architect", "security-engineer")
    assert plan.mode == "quick"
    assert any("devops-engineer" in d for d in plan.decisions)
    assert any("Always staffed" in d for d in plan.decisions)


def test_unreadable_plan_falls_back_to_the_default_quick_plan():
    for text in ("no json here", "{not json}", "[1, 2]"):
        plan = parse_plan(text)
        assert plan.mode == "quick"
        assert plan.staff == ("business-analyst", "architect", "security-engineer")
        assert plan.decisions and "default quick plan" in plan.decisions[0]


def test_next_role_is_pure_routing():
    assert next_role({"status": "needs_operator"}) == "FINISH"
    assert next_role({"status": "running"}) == "team_lead_plan"
    state = {"status": "running", "plan": {"staff": ["architect", "security-engineer"], "mode": "quick"}}
    assert next_role(state) == "architect"
    assert next_role({**state, "architecture": "a"}) == "security_engineer"


def test_write_run_writes_artifacts_and_log(tmp_path: Path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_team.py"
    spec = importlib.util.spec_from_file_location("run_team", script)
    run_team = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_team)
    result, _ = run([plan_json(), PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF])

    written = {path.name for path in run_team.write_run(result, tmp_path / "run")}

    assert {"REQUIREMENTS_PACK.md", "architecture.md", "api-spec.md", "security-report.md",
            "SIGN_OFF.md", "team-log.json"} <= written
    assert "IDEA_BRIEF.md" not in written
    log = json.loads((tmp_path / "run" / "team-log.json").read_text(encoding="utf-8"))
    assert log["status"] == "approved" and log["model_calls"] == 7

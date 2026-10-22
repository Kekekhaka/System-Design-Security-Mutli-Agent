"""The team pipeline's build stages, with the workspace faked (see test_team_workspace.py)."""

import json
from pathlib import Path

from src.agents.team.graph import (
    BUILD_NOTE,
    RUN_CONFIG,
    build_team_graph,
    initial_team_state,
)
from src.agents.team.plan import parse_plan
from src.agents.team.roles import load_roles
from src.agents.team.workspace import CommandResult, WorkspaceError
from src.core.config import settings
from tests.fakes import ScriptedLLM
from tests.test_team_graph import (
    APPROVE_ROUND,
    DESIGN_V1,
    PACK,
    REJECT_ROUND,
    SIGN_OFF,
    load_run_team,
    plan_json,
)

BUILD_STAFF = ["business-analyst", "designer", "security-engineer", "backend-engineer", "qa-analyst", "reviewer"]
REVIEW_OK = "APPROVED\nMatches the contract; readable."
REVIEW_BAD = "CHANGES REQUESTED\nPOST /pay does not validate the amount."
CODE_OK = ["Code scope notes.", "Code draft: tokens only.", "APPROVED\nNo secrets in code; amount validated."]
DOCS = "# Project docs\nRun: python -m pytest"
DEPLOY = "# Deploy plan\n1. Build the image."
PASSED = CommandResult(0, "5 passed in 0.4s")
FAILED = CommandResult(1, "FAILED tests/test_qa.py::test_pay_validates_amount - AssertionError")


class FakeWorkspace:
    def __init__(self, tests=None, qa_edits_product=False, qa_reverts_product=False,
                 escape_on=None, fail_create=None):
        self.tests = list(tests or [PASSED])
        self.test_calls = 0
        self.runs: list[tuple[str, str]] = []
        self.files: dict[str, str] = {}
        self.qa_edits_product = qa_edits_product
        self.qa_reverts_product = qa_reverts_product
        self.escape_on = escape_on  # a run title prefix that writes outside the worktree
        self.outside: list[str] = []
        self.fail_create = fail_create
        self.created = []

    def create(self, repo, run_id):
        if self.fail_create:
            raise WorkspaceError(self.fail_create)
        path = Path(repo).parent / "worktrees" / run_id
        path.mkdir(parents=True, exist_ok=True)
        self.created.append(path)
        return path, f"team/{run_id}"

    def run_opencode(self, worktree, brief, title):
        self.runs.append((title, Path(brief).read_text(encoding="utf-8")))
        if self.escape_on and title.startswith(self.escape_on):
            self.outside.append("?? src/text_utils.py")
        if title.startswith("qa-analyst"):
            self.files["tests/test_qa.py"] = f"v{len(self.runs)}"
            if self.qa_edits_product:
                self.files["app.py"] = f"qa{len(self.runs)}"
            if self.qa_reverts_product:
                self.files.pop("app.py", None)  # back to HEAD: no longer a change
        else:
            self.files["app.py"] = f"eng{len(self.runs)}"
        return CommandResult(0, f"opencode output for {title}")

    def watch(self, repos):
        return {str(repo): "\n".join(self.outside) for repo in repos}

    def snapshot(self, worktree):
        return dict(self.files)

    def changed_paths(self, worktree):
        return set(self.files)

    def diff(self, worktree):
        return "diff --git a/app.py b/app.py\n+def pay(token, amount): ...  # DIFF-MARKER"

    def run_tests(self, worktree):
        result = self.tests[min(self.test_calls, len(self.tests) - 1)]
        self.test_calls += 1
        return result


def run_build(tmp_path, responses, staff=BUILD_STAFF, workspace=None):
    llm = ScriptedLLM(responses)
    ws = workspace or FakeWorkspace()
    graph = build_team_graph(llm=llm, workspace=ws)
    state = initial_team_state(
        "Build a card payment API.", build=True, repo=str(tmp_path / "repo"), run_dir=str(tmp_path / "run")
    )
    return graph.invoke(state, config=RUN_CONFIG), llm, ws


def titles(ws):
    return [title for title, _ in ws.runs]


def test_build_happy_path(tmp_path):
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND, REVIEW_OK, *CODE_OK, DOCS, SIGN_OFF]
    result, _, ws = run_build(tmp_path, responses)

    assert result["status"] == "approved"
    assert result["build_status"] == "passed"
    assert titles(ws) == ["backend-engineer round 1", "qa-analyst round 1"]
    backend_brief = ws.runs[0][1]
    assert load_roles()["backend-engineer"].body in backend_brief
    assert BUILD_NOTE in backend_brief
    assert "POST /pay {card_number}" in backend_brief  # the approved API spec is the contract
    assert "FR-01 Pay with a card." in backend_brief
    assert "You are the only engineer on this build" in backend_brief
    assert "Do not edit product code" in ws.runs[1][1]
    assert result["opencode_runs"] == 2
    assert result["project_docs"] == DOCS  # BA pass 2
    assert result["branch"] == "team/run"
    assert "build rounds: 1 of" in result["sign_off"].splitlines()[0]
    assert result["sign_off"].startswith("Verdict: APPROVED")


def test_reviewer_and_code_audit_see_the_real_diff_and_test_run(tmp_path):
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND, REVIEW_OK, *CODE_OK, DOCS, SIGN_OFF]
    _, llm, _ = run_build(tmp_path, responses)

    reviewer_prompt = llm.calls[6][1].content
    assert "DIFF-MARKER" in reviewer_prompt and "5 passed" in reviewer_prompt
    assert load_roles()["reviewer"].body in llm.calls[6][0].content
    code_scope_prompt = llm.calls[7][1].content
    assert "IMPLEMENTATION CHANGES UNDER REVIEW" in code_scope_prompt
    assert "DIFF-MARKER" in code_scope_prompt


def test_code_audit_changes_requested_loops_back(tmp_path):
    code_bad = ["Code scope notes.", "Code draft: token in logs.", "CHANGES REQUESTED\nThe payment token is written to the request log."]
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND,
                 REVIEW_OK, *code_bad, REVIEW_OK, *CODE_OK, DOCS, SIGN_OFF]
    result, _, ws = run_build(tmp_path, responses)

    assert result["status"] == "approved" and result["build_round"] == 2
    assert "Build round 1 failed (code_audit); sent back to the engineers." in result["decisions"]
    assert "The payment token is written to the request log." in ws.runs[2][1]


def test_failing_tests_go_back_to_the_engineer_with_the_findings(tmp_path):
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND,
                 REVIEW_OK, *CODE_OK, REVIEW_OK, *CODE_OK, DOCS, SIGN_OFF]
    result, _, ws = run_build(tmp_path, responses, workspace=FakeWorkspace(tests=[FAILED, PASSED]))

    assert result["status"] == "approved"
    assert result["build_round"] == 2
    assert titles(ws) == ["backend-engineer round 1", "qa-analyst round 1",
                          "backend-engineer round 2", "qa-analyst round 2"]
    second_brief = ws.runs[2][1]
    assert "Findings from build round 1" in second_brief
    assert "FAILED tests/test_qa.py::test_pay_validates_amount" in second_brief
    assert "Build round 1 failed (tests); sent back to the engineers." in result["decisions"]


def test_review_changes_requested_loops_back(tmp_path):
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND,
                 REVIEW_BAD, *CODE_OK, REVIEW_OK, *CODE_OK, DOCS, SIGN_OFF]
    result, _, ws = run_build(tmp_path, responses)

    assert result["status"] == "approved" and result["build_round"] == 2
    assert "POST /pay does not validate the amount." in ws.runs[2][1]
    assert "Build round 1 failed (review); sent back to the engineers." in result["decisions"]


def test_build_escalates_at_the_house_revision_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MAX_REVISIONS", 2)
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND,
                 REVIEW_OK, *CODE_OK, REVIEW_OK, *CODE_OK, "Sign-off claiming APPROVED."]
    result, _, ws = run_build(tmp_path, responses, workspace=FakeWorkspace(tests=[FAILED]))

    assert result["status"] == "escalated"
    assert result["build_status"] == "failed"
    header = result["sign_off"].splitlines()[0]
    assert header.startswith("Verdict: CHANGES REQUESTED") and "failing gates: tests" in header
    assert result["project_docs"] is None  # no docs for a failed build
    assert len(ws.runs) == 4


def test_qa_editing_product_code_fails_the_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MAX_REVISIONS", 2)
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND,
                 REVIEW_OK, *CODE_OK, REVIEW_OK, *CODE_OK, SIGN_OFF]
    result, _, ws = run_build(tmp_path, responses, workspace=FakeWorkspace(qa_edits_product=True))

    assert result["qa_violations"] == ["app.py"]
    assert result["status"] == "escalated"
    assert "QA changed product files" in ws.runs[2][1] and "app.py" in ws.runs[2][1]
    assert "qa_scope" in result["sign_off"].splitlines()[0]


def test_rejected_design_is_never_built(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MAX_REVISIONS", 1)
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *REJECT_ROUND, SIGN_OFF]
    result, _, ws = run_build(tmp_path, responses)

    assert result["status"] == "escalated"
    assert ws.created == [] and ws.runs == []
    assert "design audit reached the house revision limit" in result["sign_off"].splitlines()[0]


def test_a_workspace_error_stops_the_build(tmp_path):
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF]
    ws = FakeWorkspace(fail_create="C:/x is not a git repository")
    result, _, _ = run_build(tmp_path, responses, workspace=ws)

    assert result["status"] == "escalated"
    assert ws.runs == []
    assert "could not start: C:/x is not a git repository" in result["sign_off"].splitlines()[0]


def test_frontend_engineer_builds_after_the_backend(tmp_path):
    staff = [*BUILD_STAFF, "frontend-engineer"]
    responses = [plan_json(staff=staff), PACK, DESIGN_V1, *APPROVE_ROUND, REVIEW_OK, *CODE_OK, DOCS, SIGN_OFF]
    _, _, ws = run_build(tmp_path, responses, staff=staff)

    assert titles(ws) == ["backend-engineer round 1", "frontend-engineer round 1", "qa-analyst round 1"]
    assert "the frontend engineer builds the presentation after you" in ws.runs[0][1]
    assert load_roles()["frontend-engineer"].body in ws.runs[1][1]


def test_devops_writes_the_deploy_plan_after_a_passing_build(tmp_path):
    staff = [*BUILD_STAFF, "devops-engineer"]
    responses = [plan_json(staff=staff), PACK, DESIGN_V1, *APPROVE_ROUND, REVIEW_OK, *CODE_OK, DEPLOY, DOCS, SIGN_OFF]
    result, _, _ = run_build(tmp_path, responses, staff=staff)

    assert result["deploy_plan"] == DEPLOY
    assert result["status"] == "approved"


def test_a_build_plan_always_has_an_engineer_qa_and_a_reviewer():
    plan = parse_plan(plan_json(staff=["designer", "security-engineer"]), build=True)

    assert plan.staff == ("designer", "security-engineer", "backend-engineer", "qa-analyst", "reviewer")
    assert any("added backend-engineer" in d for d in plan.decisions)
    assert any("tested and reviewed" in d for d in plan.decisions)


def test_write_run_writes_the_build_artifacts(tmp_path):
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND, REVIEW_OK, *CODE_OK, DOCS, SIGN_OFF]
    result, _, _ = run_build(tmp_path, responses)

    written = {p.name for p in load_run_team().write_run(result, tmp_path / "out")}

    assert {"TEST_REPORT.md", "REVIEW_REPORT.md", "code-security-report.md", "changes.diff",
            "PROJECT_DOCS.md", "SIGN_OFF.md", "team-log.json"} <= written
    log = json.loads((tmp_path / "out" / "team-log.json").read_text(encoding="utf-8"))
    assert log["build"]["status"] == "passed" and log["build"]["branch"] == "team/run"
    assert log["opencode_runs"] == 2
    assert "PASSED" in (tmp_path / "out" / "TEST_REPORT.md").read_text(encoding="utf-8")


def test_a_write_outside_the_worktree_stops_the_build_at_once(tmp_path):
    # Seen live: a nested worktree let the engineer write into the agent project.
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND, SIGN_OFF]
    ws = FakeWorkspace(escape_on="backend-engineer")
    result, _, _ = run_build(tmp_path, responses, workspace=ws)

    assert result["status"] == "escalated"
    assert result["build_status"] == "failed"
    assert titles(ws) == ["backend-engineer round 1"], "no QA, review or second round after a breach"
    assert any("src/text_utils.py" in v for v in result["isolation_violations"])
    assert "isolation" in result["sign_off"].splitlines()[0]
    assert any("wrote outside the build worktree" in d for d in result["decisions"])
    # Stopped in the round it happened, not counted up to the revision limit.
    assert result["build_round"] == 1
    assert any(d.startswith("Build stopped: a role wrote outside the worktree") for d in result["decisions"])


def test_qa_reverting_product_code_fails_the_gate(tmp_path, monkeypatch):
    # The engineer's app.py change disappears during QA's run: still a QA violation.
    monkeypatch.setattr(settings, "MAX_REVISIONS", 1)
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND, REVIEW_OK, *CODE_OK, SIGN_OFF]
    result, _, _ = run_build(tmp_path, responses, workspace=FakeWorkspace(qa_reverts_product=True))

    assert result["qa_violations"] == ["app.py"]
    assert result["status"] == "escalated"


def test_each_opencode_run_saves_its_output_next_to_its_brief(tmp_path):
    responses = [plan_json(staff=BUILD_STAFF), PACK, DESIGN_V1, *APPROVE_ROUND, REVIEW_OK, *CODE_OK, DOCS, SIGN_OFF]
    run_build(tmp_path, responses)

    briefs = tmp_path / "run" / "briefs"
    assert (briefs / "round-1-backend-engineer.log").read_text(encoding="utf-8") == (
        "opencode output for backend-engineer round 1"
    )
    assert (briefs / "round-1-qa-analyst.log").exists()

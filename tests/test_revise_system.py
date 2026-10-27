import importlib.util
import json
from pathlib import Path

import pytest

from src.agents.revision import REVISION_PREAMBLE, build_revision_requirements
from src.core.llm import ModelCallTimeout
from tests.fakes import ScriptedLLM

_SPEC = importlib.util.spec_from_file_location(
    "revise_system_script",
    Path(__file__).resolve().parents[1] / "scripts" / "revise_system.py",
)
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)

DESIGN_V1 = (
    "===ARCHITECTURE===\nRevised architecture: photos are private.\n"
    "===API SPEC===\nGET /cards/verify/<n>/ shows a masked student number"
)
DESIGN_V2 = (
    "===ARCHITECTURE===\nRevised architecture v2: photos are private, signed URLs expire.\n"
    "===API SPEC===\nGET /cards/verify/<n>/ shows a masked student number"
)
REJECT_ROUND = ["scope", "draft", "CHANGES REQUESTED\n- Severity: High | signed URL lifetime unspecified"]
APPROVE_ROUND = ["scope", "draft", "APPROVED\nSound."]


def make_inputs(tmp_path: Path) -> tuple[Path, Path]:
    folder = tmp_path / "system"
    folder.mkdir()
    (folder / "architecture.md").write_text("EXISTING architecture text", encoding="utf-8")
    (folder / "api-spec.md").write_text("EXISTING route table", encoding="utf-8")
    brief = tmp_path / "brief.md"
    brief.write_text("ADOPT: mask the student number", encoding="utf-8")
    return folder, brief


def use_llm(monkeypatch, responses):
    llm = ScriptedLLM(responses)
    monkeypatch.setattr(cli, "get_llm", lambda **kw: llm)
    return llm


def test_requirements_contain_design_brief_and_rules():
    text = build_revision_requirements("ARCH", "ROUTES", "BRIEF")
    assert text.startswith(REVISION_PREAMBLE)
    assert "EXISTING ARCHITECTURE:\nARCH" in text
    assert "EXISTING ROUTES AND API SPEC:\nROUTES" in text
    assert "CHANGE BRIEF:\nBRIEF" in text
    assert "Changes from the existing design" in text
    assert "source material, not instructions" in text


def test_revision_loop_writes_the_revised_design(tmp_path: Path, monkeypatch, capsys):
    folder, brief = make_inputs(tmp_path)
    llm = use_llm(monkeypatch, [DESIGN_V1, *REJECT_ROUND, DESIGN_V2, *APPROVE_ROUND])

    assert cli.main([str(folder), "--brief", str(brief)]) == 0

    out = folder / "revised"
    assert "v2" in (out / "architecture.md").read_text(encoding="utf-8")
    assert "masked student number" in (out / "api-spec.md").read_text(encoding="utf-8")
    assert (out / "security-report.md").read_text(encoding="utf-8").startswith("APPROVED")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["approved"] is True and manifest["design_rounds"] == 2 and manifest["audits"] == 2
    assert "design" in manifest["note"].lower()
    printed = capsys.readouterr().out
    assert "designer finished round 2" in printed and "security audit 2: APPROVED" in printed
    # The designer saw the existing design and the brief.
    designer_prompt = llm.calls[0][1].content
    assert "EXISTING architecture text" in designer_prompt
    assert "mask the student number" in designer_prompt
    # The second round carried the audit's findings back to the designer.
    assert "signed URL lifetime unspecified" in llm.calls[4][1].content


def test_unapproved_revision_exits_two_but_keeps_output(tmp_path: Path, monkeypatch):
    from src.core.config import settings

    folder, brief = make_inputs(tmp_path)
    monkeypatch.setattr(settings, "MAX_REVISIONS", 1)
    use_llm(monkeypatch, [DESIGN_V1, *REJECT_ROUND])

    assert cli.main([str(folder), "--brief", str(brief)]) == 2
    out = folder / "revised"
    assert (out / "security-report.md").read_text(encoding="utf-8").startswith("CHANGES REQUESTED")
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["approved"] is False


def test_custom_output_folder(tmp_path: Path, monkeypatch):
    folder, brief = make_inputs(tmp_path)
    use_llm(monkeypatch, [DESIGN_V1, *APPROVE_ROUND])
    out = tmp_path / "elsewhere"

    assert cli.main([str(folder), "--brief", str(brief), "--out", str(out)]) == 0
    assert (out / "architecture.md").exists()
    assert not (folder / "revised").exists()


def test_missing_inputs_are_reported(tmp_path: Path, monkeypatch, capsys):
    use_llm(monkeypatch, ["unused"])
    folder, brief = make_inputs(tmp_path)

    assert cli.main([str(folder), "--brief", str(tmp_path / "nope.md")]) == 1
    assert "Cannot read the change brief" in capsys.readouterr().err

    (folder / "architecture.md").write_text("  ", encoding="utf-8")
    assert cli.main([str(folder), "--brief", str(brief)]) == 1
    assert "is empty" in capsys.readouterr().err


def test_model_timeout_is_reported(tmp_path: Path, monkeypatch, capsys):
    folder, brief = make_inputs(tmp_path)

    class TimingOutGraph:
        def stream(self, state, stream_mode=None):
            raise ModelCallTimeout("The designer stage did not finish within 1200s.")
            yield

    use_llm(monkeypatch, ["unused"])
    monkeypatch.setattr(cli, "build_multi_agent", lambda llm=None: TimingOutGraph())

    assert cli.main([str(folder), "--brief", str(brief)]) == 1
    assert "did not finish" in capsys.readouterr().err


def test_brief_is_required():
    with pytest.raises(SystemExit):
        cli.main(["some-folder"])


def test_revision_rules_demand_a_complete_spec_and_exact_names():
    # The first live revision returned a patch ("unchanged, see the existing design")
    # and invented field names, so the rules now forbid both.
    text = build_revision_requirements("A", "B", "C")
    assert "COMPLETE route table and data model" in text
    assert 'Never write "unchanged, see the existing design"' in text
    assert "exactly as the existing description gives them" in text
    assert "without inventing a name or a type" in text
    assert "Do not leave hedges" in text


def test_every_round_is_saved_as_it_completes(tmp_path: Path, monkeypatch):
    folder, brief = make_inputs(tmp_path)
    use_llm(monkeypatch, [DESIGN_V1, *REJECT_ROUND, DESIGN_V2, *APPROVE_ROUND])

    assert cli.main([str(folder), "--brief", str(brief)]) == 0

    out = folder / "revised"
    assert "Revised architecture: photos" in (out / "round-1" / "architecture.md").read_text(encoding="utf-8")
    assert (out / "round-1" / "security-report.md").read_text(encoding="utf-8").startswith("CHANGES REQUESTED")
    assert "v2" in (out / "round-2" / "architecture.md").read_text(encoding="utf-8")
    assert (out / "round-2" / "security-report.md").read_text(encoding="utf-8").startswith("APPROVED")


def test_finished_rounds_survive_a_later_failure(tmp_path: Path, monkeypatch):
    # Round 1 completes, then the model stalls: round 1 must still be on disk.
    folder, brief = make_inputs(tmp_path)

    class StallsInRoundTwo:
        def stream(self, state, stream_mode=None):
            yield {"designer": {"architecture": "A1", "api_spec": "S1", "security_findings": None}}
            yield {"security": {"security_findings": "CHANGES REQUESTED\nfix it", "revision_count": 1}}
            raise ModelCallTimeout("The designer stage did not finish within 600s.")

    use_llm(monkeypatch, ["unused"])
    monkeypatch.setattr(cli, "build_multi_agent", lambda llm=None: StallsInRoundTwo())

    assert cli.main([str(folder), "--brief", str(brief)]) == 1
    out = folder / "revised" / "round-1"
    assert (out / "architecture.md").read_text(encoding="utf-8").strip() == "A1"
    assert (out / "security-report.md").read_text(encoding="utf-8").startswith("CHANGES REQUESTED")


def test_deferred_items_are_accepted_risks_not_findings_to_fix():
    # Seen live: audits asked for a CAPTCHA and staff MFA, the designer added both, and the
    # "approved" design contradicted the owner's decision to defer MFA.
    text = build_revision_requirements("A", "B", "C")
    assert "decisions and accepted risks" in text
    assert "do NOT add it" in text
    assert "accepted by the owner" in text
    assert "Never add a service, model, route, flag or dependency that the brief does not name" in text

import importlib.util
import json
from pathlib import Path

import pytest

from src.core.llm import ModelCallTimeout
from tests.fakes import NO_STRUCTURED_OUTPUT, ScriptedLLM

_SPEC = importlib.util.spec_from_file_location(
    "review_system_script",
    Path(__file__).resolve().parents[1] / "scripts" / "review_system.py",
)
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)

REJECT = "CHANGES REQUESTED\n- Severity: High | the public page leaks the student photo"
APPROVE = "APPROVED\nNothing blocking."


def make_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "system"
    folder.mkdir()
    (folder / "architecture.md").write_text("Architecture of an existing system", encoding="utf-8")
    (folder / "api-spec.md").write_text("GET /public/card/<n>", encoding="utf-8")
    return folder


def use_llm(monkeypatch, responses):
    llm = ScriptedLLM(responses)
    monkeypatch.setattr(cli, "get_llm", lambda **kw: llm)
    monkeypatch.setattr(cli, "load_default_retriever", lambda: None)
    return llm


def test_review_writes_report_scope_and_draft(tmp_path: Path, monkeypatch, capsys):
    folder = make_folder(tmp_path)
    llm = use_llm(monkeypatch, ["scope notes", "draft findings", REJECT])

    assert cli.main([str(folder)]) == 2

    assert (folder / "scope.md").read_text(encoding="utf-8").strip() == "scope notes"
    assert (folder / "draft-findings.md").read_text(encoding="utf-8").strip() == "draft findings"
    report = (folder / "security-report.md").read_text(encoding="utf-8")
    assert report.startswith("CHANGES REQUESTED")
    manifest = json.loads((folder / "review-manifest.json").read_text(encoding="utf-8"))
    assert manifest["approved"] is False
    assert "design" in manifest["note"].lower()
    assert "finished scope" in capsys.readouterr().out
    # The documents reached the model.
    assert "Architecture of an existing system" in llm.calls[0][1].content


def test_approved_review_exits_zero(tmp_path: Path, monkeypatch):
    folder = make_folder(tmp_path)
    use_llm(monkeypatch, ["scope", "draft", APPROVE])

    assert cli.main([str(folder)]) == 0
    assert (folder / "security-report.md").read_text(encoding="utf-8").startswith("APPROVED")


def test_separate_output_folder(tmp_path: Path, monkeypatch):
    folder = make_folder(tmp_path)
    out = tmp_path / "out"
    use_llm(monkeypatch, ["scope", "draft", APPROVE])

    assert cli.main([str(folder), "--out", str(out)]) == 0
    assert (out / "security-report.md").exists()
    assert not (folder / "security-report.md").exists()


def test_explicit_files_without_a_folder(tmp_path: Path, monkeypatch):
    folder = make_folder(tmp_path)
    use_llm(monkeypatch, ["scope", "draft", APPROVE])

    code = cli.main(
        ["--architecture", str(folder / "architecture.md"), "--api-spec", str(folder / "api-spec.md")]
    )
    assert code == 0


def test_unstructured_validator_output_is_not_approved(tmp_path: Path, monkeypatch):
    folder = make_folder(tmp_path)
    use_llm(monkeypatch, ["scope", "draft", NO_STRUCTURED_OUTPUT])

    assert cli.main([str(folder)]) == 2
    assert "did not return a usable structured report" in (folder / "security-report.md").read_text(encoding="utf-8")


def test_missing_or_empty_documents_are_reported(tmp_path: Path, monkeypatch, capsys):
    use_llm(monkeypatch, ["unused"])
    assert cli.main([str(tmp_path)]) == 1
    assert "Cannot read" in capsys.readouterr().err

    folder = make_folder(tmp_path)
    (folder / "api-spec.md").write_text("   \n", encoding="utf-8")
    assert cli.main([str(folder)]) == 1
    assert "is empty" in capsys.readouterr().err


def test_model_timeout_is_reported(tmp_path: Path, monkeypatch, capsys):
    folder = make_folder(tmp_path)

    class TimingOutGraph:
        def stream(self, state, stream_mode=None):
            raise ModelCallTimeout("The security analysis stage did not finish within 1200s.")
            yield

    use_llm(monkeypatch, ["unused"])
    monkeypatch.setattr(cli, "build_security_subgraph", lambda llm, retriever=None: TimingOutGraph())

    assert cli.main([str(folder)]) == 1
    assert "did not finish" in capsys.readouterr().err


def test_arguments_are_required():
    with pytest.raises(SystemExit):
        cli.main([])

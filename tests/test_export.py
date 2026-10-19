import importlib.util
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from src.core.llm import ModelOutputError
from src.export.exporter import (
    export_design,
    strip_code_fence,
    validate_openapi,
    validate_sql,
    write_export,
)
from src.export.prompts import OPENAPI_SYSTEM_PROMPT, SQL_SYSTEM_PROMPT

_SPEC = importlib.util.spec_from_file_location(
    "export_design_script",
    Path(__file__).resolve().parents[1] / "scripts" / "export_design.py",
)
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)

GOOD_OPENAPI = """\
openapi: 3.1.0
info:
  title: Guest House API
  version: 1.0.0
components:
  securitySchemes:
    bearer:
      type: http
      scheme: bearer
  schemas:
    Room:
      type: object
      properties:
        id: {type: string}
paths:
  /rooms:
    get:
      security: [{bearer: []}]
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: "#/components/schemas/Room"
  /health:
    get:
      security: []
      responses:
        "200": {description: ok}
"""

GOOD_SQL = """\
-- rooms
CREATE TABLE rooms (
    id uuid PRIMARY KEY,
    name text NOT NULL CHECK (length(name) > 0)
);
CREATE INDEX rooms_name_idx ON rooms (name);
"""

DESIGN = {
    "approved": True,
    "total_revisions": 2,
    "final_architecture": "Architecture text",
    "api_spec": "GET /rooms ...",
    "security_audit": "APPROVED\nfine",
}


class KeyedLLM:
    """Answers by system prompt, so the parallel export calls cannot be mixed up."""

    def __init__(self, openapi: str, sql: str) -> None:
        self.openapi, self.sql = openapi, sql
        self.calls: list[list] = []

    def invoke(self, messages, **kwargs) -> AIMessage:
        self.calls.append(messages)
        system = messages[0].content
        if system == OPENAPI_SYSTEM_PROMPT:
            return AIMessage(content=self.openapi)
        assert system == SQL_SYSTEM_PROMPT
        return AIMessage(content=self.sql)


def test_strip_code_fence():
    assert strip_code_fence("```yaml\na: 1\n```") == "a: 1"
    assert strip_code_fence("Here you go:\n```sql\nSELECT 1;\n```\nDone.") == "SELECT 1;"
    assert strip_code_fence("  plain: text  ") == "plain: text"
    short, long = "```\nx\n```", "```\nlonger block\n```"
    assert strip_code_fence(f"{short}\n{long}") == "longer block"


def test_valid_openapi_has_no_problems():
    assert validate_openapi(GOOD_OPENAPI) == []


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda s: s.replace("openapi: 3.1.0", "swagger: '2.0'"), "openapi"),
        (lambda s: s.replace('"#/components/schemas/Room"', '"#/components/schemas/Gone"'), "unresolved $ref"),
        (lambda s: s.replace("      security: [{bearer: []}]\n", ""), "no security requirement"),
        (lambda s: s.replace("  securitySchemes:\n    bearer:\n      type: http\n      scheme: bearer\n", "  x: {}\n"), "securitySchemes"),
        (lambda s: s.replace('        "200": {description: ok}', "        {}"), "no responses"),
    ],
)
def test_openapi_problems_are_reported(mutate, expected):
    problems = validate_openapi(mutate(GOOD_OPENAPI))
    assert any(expected in problem for problem in problems), problems


def test_openapi_not_yaml_or_not_a_mapping():
    assert "not valid YAML" in validate_openapi("a: [unclosed")[0]
    assert "not a YAML mapping" in validate_openapi("- just\n- a list")[0]


def test_global_security_covers_operations():
    doc = GOOD_OPENAPI.replace("      security: [{bearer: []}]\n", "").replace(
        "paths:", "security: [{bearer: []}]\npaths:"
    )
    assert validate_openapi(doc) == []


def test_valid_sql_has_no_problems():
    assert validate_sql(GOOD_SQL) == []


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT 1;", "no CREATE TABLE"),
        ("CREATE TABLE a (id int;", "unbalanced"),
        ("CREATE TABLE a (id int);\nDROP TABLE a;", "destructive"),
        ("CREATE TABLE a (id int)", "not terminated"),
    ],
)
def test_sql_problems_are_reported(sql, expected):
    assert any(expected in problem for problem in validate_sql(sql))


def test_sql_checks_ignore_comments_and_strings():
    sql = "-- DROP TABLE x (\nCREATE TABLE a (note text DEFAULT 'DROP ( me');\n"
    assert validate_sql(sql) == []


def test_export_design_runs_both_conversions_and_checks_them():
    llm = KeyedLLM(f"```yaml\n{GOOD_OPENAPI}```", f"```sql\n{GOOD_SQL}```")

    result = export_design(llm, DESIGN)

    assert result.openapi_yaml.startswith("openapi: 3.1.0")
    assert result.schema_sql.startswith("-- rooms")
    assert result.problems == {"openapi": [], "schema": []}
    assert not result.flagged
    assert len(llm.calls) == 2
    for call in llm.calls:
        assert "Architecture text" in call[1].content
        assert "GET /rooms" in call[1].content


def test_export_design_flags_bad_output_without_hiding_it():
    llm = KeyedLLM("openapi: 3.1.0\ninfo: {title: x}\npaths: {}", "SELECT 1;")

    result = export_design(llm, DESIGN)

    assert result.flagged
    assert "no paths defined" in result.problems["openapi"]
    assert "no CREATE TABLE statement" in result.problems["schema"]


def test_empty_model_output_raises():
    llm = KeyedLLM("", GOOD_SQL)
    with pytest.raises(ModelOutputError, match="openapi export"):
        export_design(llm, DESIGN)


def test_design_without_content_is_rejected():
    with pytest.raises(ValueError):
        export_design(KeyedLLM("", ""), {"approved": True, "final_architecture": "", "api_spec": ""})


def test_write_export_writes_documents_and_manifest(tmp_path: Path):
    result = export_design(KeyedLLM(GOOD_OPENAPI, GOOD_SQL), DESIGN)

    paths = write_export(result, DESIGN, tmp_path / "out")

    assert sorted(p.name for p in paths) == [
        "api-spec.md",
        "design.md",
        "manifest.json",
        "openapi.yaml",
        "schema.sql",
        "security-audit.md",
    ]
    assert (tmp_path / "out" / "design.md").read_text(encoding="utf-8") == "Architecture text\n"
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["design_approved"] is True
    assert manifest["total_revisions"] == 2
    assert manifest["validation"] == {"openapi": [], "schema": []}
    assert "LLM-generated" in manifest["note"]


def write_design(tmp_path: Path, design: dict, bom: bool = False) -> Path:
    path = tmp_path / "result.json"
    path.write_text(json.dumps(design), encoding="utf-8-sig" if bom else "utf-8")
    return path


def test_cli_refuses_unapproved_designs(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_llm", lambda **kw: pytest.fail("must not call the model"))
    path = write_design(tmp_path, {**DESIGN, "approved": False})

    assert cli.main([str(path), "--out", str(tmp_path / "out")]) == 1
    assert "did not approve" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_cli_exports_a_bom_prefixed_result(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "get_llm", lambda **kw: KeyedLLM(GOOD_OPENAPI, GOOD_SQL))
    path = write_design(tmp_path, DESIGN, bom=True)  # PowerShell writes a BOM

    assert cli.main([str(path), "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "openapi.yaml").exists()


def test_cli_exit_code_2_when_checks_flag_problems(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_llm", lambda **kw: KeyedLLM("openapi: 3.1.0", "SELECT 1;"))
    path = write_design(tmp_path, DESIGN)

    assert cli.main([str(path), "--out", str(tmp_path / "out")]) == 2
    assert "[openapi]" in capsys.readouterr().err
    assert (tmp_path / "out" / "manifest.json").exists()


def test_cli_allow_unapproved_flag(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "get_llm", lambda **kw: KeyedLLM(GOOD_OPENAPI, GOOD_SQL))
    path = write_design(tmp_path, {**DESIGN, "approved": False})

    assert cli.main([str(path), "--out", str(tmp_path / "out"), "--allow-unapproved"]) == 0
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["design_approved"] is False


def test_cli_missing_file(tmp_path: Path, capsys):
    assert cli.main([str(tmp_path / "nope.json")]) == 1
    assert "Cannot read" in capsys.readouterr().err


def test_sqlglot_syntax_check_when_installed():
    pytest.importorskip("sqlglot")

    assert validate_sql(GOOD_SQL) == []
    # Parentheses balance, so only a real parse notices the typo'd keyword.
    problems = validate_sql("CREAT TABLE a (id int);\nCREATE TABLE b (id int);")
    assert any("does not parse as PostgreSQL" in problem for problem in problems)


def test_cli_uses_export_token_and_reasoning_settings(tmp_path: Path, monkeypatch):
    from src.core.config import settings

    seen = {}

    def fake_get_llm(**kwargs):
        seen.update(kwargs)
        return KeyedLLM(GOOD_OPENAPI, GOOD_SQL)

    monkeypatch.setattr(cli, "get_llm", fake_get_llm)
    monkeypatch.setattr(settings, "EXPORT_REASONING_EFFORT", "low")
    monkeypatch.setattr(settings, "EXPORT_MAX_TOKENS", 64000)
    path = write_design(tmp_path, DESIGN)

    assert cli.main([str(path), "--out", str(tmp_path / "out")]) == 0
    assert seen == {"temperature": 0.0, "max_tokens": 64000, "reasoning_effort": "low"}


def test_cli_reports_a_model_timeout(tmp_path: Path, monkeypatch, capsys):
    from src.core.llm import ModelCallTimeout

    def timed_out(llm, design):
        raise ModelCallTimeout("The openapi export stage did not finish within 1200s.")

    monkeypatch.setattr(cli, "get_llm", lambda **kw: KeyedLLM(GOOD_OPENAPI, GOOD_SQL))
    monkeypatch.setattr(cli, "export_design", timed_out)
    path = write_design(tmp_path, DESIGN)

    assert cli.main([str(path), "--out", str(tmp_path / "out")]) == 1
    assert "did not finish" in capsys.readouterr().err

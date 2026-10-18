import importlib.util
import json
from pathlib import Path

from tests.fakes import ScriptedLLM

_SPEC = importlib.util.spec_from_file_location(
    "eval_security_agent",
    Path(__file__).resolve().parents[1] / "scripts" / "eval_security_agent.py",
)
eval_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eval_module)


class FakeLLM(ScriptedLLM):
    def __init__(self, response: str) -> None:
        super().__init__([response])


VULNERABLE_FIXTURE = {
    "id": "test-vuln",
    "expected": "reject",
    "planted_flaws": ["flaw one", "flaw two"],
    "architecture": "===ARCHITECTURE===\nraw card data stored in db",
    "api_spec": "===API SPEC===\nPOST /pay",
}

SAFE_FIXTURE = {
    "id": "test-safe",
    "expected": "approve",
    "planted_flaws": [],
    "architecture": "===ARCHITECTURE===\ntokenized payments",
    "api_spec": "===API SPEC===\nPOST /pay {token}",
}


def test_parse_json_object_variants():
    fenced = 'Sure:\n```json\n{"a": 1}\n```\nDone.'
    assert eval_module.parse_json_object(fenced) == {"a": 1}
    assert eval_module.parse_json_object('{"b": true}') == {"b": True}
    assert eval_module.parse_json_object("no json here") is None
    assert eval_module.parse_json_object("[1, 2, 3]") is None


def test_build_judge_prompt_marks_safe_and_vulnerable():
    safe = eval_module.build_judge_prompt(SAFE_FIXTURE, "APPROVED", "APPROVED")
    assert "(none - this design is safe)" in safe
    assert "approve (APPROVED)" in safe

    vuln = eval_module.build_judge_prompt(VULNERABLE_FIXTURE, "CHANGES", "CHANGES REQUESTED")
    assert "0. flaw one" in vuln
    assert "1. flaw two" in vuln
    assert "reject (CHANGES REQUESTED)" in vuln


def test_run_case_validates_flaw_indexes_and_scores_verdict():
    judge = FakeLLM(
        json.dumps(
            {
                "verdict_correct": True,
                "flaws_caught": [0, 1, 7, "0", True],
                "hallucinated_findings": ["invented issue"],
                "notes": "ok",
            }
        )
    )
    agent = FakeLLM("CHANGES REQUESTED\n- flaw one detected")

    result = eval_module.run_case(agent, judge, VULNERABLE_FIXTURE, "persona")

    assert result["verdict"] == "CHANGES REQUESTED"
    assert result["verdict_correct"] is True
    assert result["flaws_planted"] == 2
    assert result["flaws_caught"] == 2
    assert result["hallucinated_findings"] == ["invented issue"]
    assert "judge_error" not in result
    assert "persona" in [call[0].content for call in agent.calls]


def test_run_case_records_judge_failure():
    agent = FakeLLM("APPROVED\nlooks fine")
    judge = FakeLLM("I cannot evaluate this")

    result = eval_module.run_case(agent, judge, SAFE_FIXTURE, "persona")

    assert result["verdict_correct"] is True
    assert "judge_error" in result


def test_summarize_excludes_judge_failures():
    cases = [
        {
            "expected": "reject",
            "verdict": "CHANGES REQUESTED",
            "verdict_correct": True,
            "flaws_planted": 2,
            "flaws_caught": 1,
            "hallucinated_findings": [],
        },
        {
            "expected": "approve",
            "verdict": "APPROVED",
            "verdict_correct": True,
            "flaws_planted": 0,
            "flaws_caught": 0,
            "hallucinated_findings": ["noise"],
        },
        {
            "expected": "approve",
            "verdict": "APPROVED",
            "verdict_correct": True,
            "judge_error": "unparseable",
        },
    ]

    summary = eval_module.summarize(cases)

    assert summary["cases"] == 3
    assert summary["verdict_accuracy"] == 1.0
    assert summary["flaw_detection_rate"] == 0.5
    assert summary["cases_with_hallucinated_findings"] == 1
    assert summary["judge_failures"] == 1
    assert summary["safe_cases_approved"] == "2/2"

"""Every model call goes through OpenRouter: the guards that keep it that way."""

import subprocess
from pathlib import Path

import pytest

from src.agents.team.workspace import GitWorkspace, WorkspaceError
from src.core import llm as llm_module
from src.core.config import Settings, settings

MAIN_KEY = "sk-or-main-key"
BUILD_KEY = "sk-or-dedicated-build-key"


# --- the base URL guard -------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://openrouter.ai/api/v1",
        "https://eu.openrouter.ai/api/v1",
        "HTTPS://OpenRouter.AI/api/v1",
    ],
)
def test_openrouter_urls_are_accepted(url):
    llm_module.require_openrouter(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "https://api.openai.com/v1",
        "https://openrouter.ai.example.com/v1",  # look-alike host
        "https://evil-openrouter.ai/v1",  # look-alike host
        "https://example.com/openrouter.ai/",  # name only in the path
        "",
    ],
)
def test_other_endpoints_are_refused(url):
    with pytest.raises(RuntimeError, match="not OpenRouter"):
        llm_module.require_openrouter(url)


def test_get_llm_refuses_a_non_openrouter_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "dummy-key")
    monkeypatch.setattr(settings, "OPENROUTER_BASE_URL", "http://localhost:11434/v1")
    with pytest.raises(RuntimeError, match="not OpenRouter"):
        llm_module.get_llm()


def test_the_opt_out_allows_another_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "dummy-key")
    monkeypatch.setattr(settings, "OPENROUTER_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(settings, "ALLOW_NON_OPENROUTER", True)
    assert llm_module.get_llm() is not None


def test_defaults_keep_everything_on_openrouter():
    assert Settings.model_fields["ALLOW_NON_OPENROUTER"].default is False
    assert Settings.model_fields["OPENROUTER_BASE_URL"].default == "https://openrouter.ai/api/v1"
    assert Settings.model_fields["OPENCODE_MODEL"].default.startswith("openrouter/")
    assert Settings.model_fields["OPENCODE_OPENROUTER_API_KEY"].default == ""


# --- the build workspace (opencode) --------------------------------------------

def build_workspace(**overrides) -> GitWorkspace:
    options = {
        "opencode_bin": "opencode",
        "opencode_model": "openrouter/deepseek/deepseek-v4-flash",
        "engineer_timeout": 60,
        "test_command": "pytest",
        "test_timeout": 60,
        "diff_max_chars": 1000,
        "opencode_api_key": BUILD_KEY,
        "enforce_openrouter": True,
    }
    options.update(overrides)
    return GitWorkspace(**options)


@pytest.fixture(autouse=True)
def main_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", MAIN_KEY)
    monkeypatch.setattr(settings, "ALLOW_NON_OPENROUTER", False)


def test_a_correct_setup_passes():
    build_workspace().check_openrouter()


def test_a_non_openrouter_model_is_refused():
    with pytest.raises(WorkspaceError, match="not an OpenRouter model"):
        build_workspace(opencode_model="opencode/deepseek-v4.1-flash-free").check_openrouter()


def test_a_missing_build_key_is_refused():
    with pytest.raises(WorkspaceError, match="OPENCODE_OPENROUTER_API_KEY is not set"):
        build_workspace(opencode_api_key="").check_openrouter()


def test_the_main_key_cannot_be_used_for_builds():
    with pytest.raises(WorkspaceError, match="same as OPENROUTER_API_KEY"):
        build_workspace(opencode_api_key=MAIN_KEY).check_openrouter()


def test_the_check_is_off_unless_enforced():
    # Direct construction (unit tests, other tools) is not enforced; from_settings is.
    build_workspace(opencode_model="anything", opencode_api_key="", enforce_openrouter=False).check_openrouter()


def test_the_opt_out_skips_the_check(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_NON_OPENROUTER", True)
    build_workspace(opencode_model="opencode/free", opencode_api_key="").check_openrouter()


def test_create_checks_before_touching_git():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    ws = build_workspace(opencode_model="opencode/free", runner=runner)
    with pytest.raises(WorkspaceError, match="not an OpenRouter model"):
        ws.create(Path("."), "run-x")
    assert calls == [], "nothing may run before the OpenRouter check passes"


def test_from_settings_enforces_and_carries_the_build_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENCODE_MODEL", "openrouter/deepseek/deepseek-v4-flash")
    monkeypatch.setattr(settings, "OPENCODE_OPENROUTER_API_KEY", BUILD_KEY)

    ws = GitWorkspace.from_settings()

    assert ws.enforce_openrouter is True
    assert ws.opencode_api_key == BUILD_KEY
    ws.check_openrouter()


def test_only_the_opencode_process_gets_the_build_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", MAIN_KEY)  # the shell's key, stripped by safe_env
    seen = {}

    def runner(command, **kwargs):
        label = "opencode" if command[0] == "opencode" else "other"
        seen[label] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    ws = build_workspace(runner=runner)
    brief = tmp_path / "brief.md"
    brief.write_text("Build it.", encoding="utf-8")

    ws.run_opencode(tmp_path, brief, "backend round 1")
    ws.run_tests(tmp_path)

    assert seen["opencode"]["OPENROUTER_API_KEY"] == BUILD_KEY
    assert "OPENROUTER_API_KEY" not in seen["other"], "the tests must never see a key"
    for env in seen.values():
        assert MAIN_KEY not in env.values(), "the main key must never reach the build"


def test_without_a_build_key_opencode_gets_no_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", MAIN_KEY)
    seen = {}

    def runner(command, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    ws = build_workspace(runner=runner, opencode_api_key="", enforce_openrouter=False)
    brief = tmp_path / "brief.md"
    brief.write_text("x", encoding="utf-8")
    ws.run_opencode(tmp_path, brief, "t")

    assert "OPENROUTER_API_KEY" not in seen["env"]

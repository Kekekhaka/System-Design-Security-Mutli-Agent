"""The build workspace against real git; only opencode is faked."""

import subprocess
import sys
from pathlib import Path

import pytest

from src.agents.team.workspace import (
    OPENCODE_MESSAGE,
    GitWorkspace,
    WorkspaceError,
    is_test_path,
    safe_env,
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    (root / ".env").write_text("OPENROUTER_API_KEY=real-secret\n", encoding="utf-8")
    return root


def workspace(root: Path, runner=subprocess.run, **overrides) -> GitWorkspace:
    options = {
        "worktree_root": root,
        "opencode_bin": "opencode", "opencode_model": "opencode/test-model", "engineer_timeout": 60,
        "test_command": f'"{sys.executable}" -c "import sys; sys.exit(0)"', "test_timeout": 60,
        "diff_max_chars": 60000,
    }
    options.update(overrides)
    return GitWorkspace(runner=runner, **options)


def test_worktree_is_isolated_on_its_own_branch(repo: Path, tmp_path: Path):
    before = git(repo, "branch", "--show-current").strip()
    path, branch = workspace(tmp_path / 'wt').create(repo, "run-1")

    assert branch == "team/run-1"
    assert path == (tmp_path / "wt" / "repo-run-1").resolve()
    assert repo not in path.parents, "the worktree must not sit inside the repository"
    assert (path / "app.py").exists()
    assert not (path / ".env").exists(), "untracked secrets must not reach the worktree"
    assert git(path, "branch", "--show-current").strip() == "team/run-1"
    assert git(repo, "branch", "--show-current").strip() == before, "the operator's checkout is untouched"


def test_create_refuses_a_folder_that_is_not_a_repo(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError, match="not a git repository"):
        workspace(tmp_path / 'wt').create(plain, 'x')


def test_create_refuses_a_repo_without_commits(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    with pytest.raises(WorkspaceError, match="no commits"):
        workspace(tmp_path / 'wt').create(empty, 'x')


def test_safe_env_strips_secrets_and_keeps_the_rest():
    env = safe_env({
        "PATH": "p", "APPDATA": "a", "USERPROFILE": "u",
        "OPENROUTER_API_KEY": "s", "GITHUB_TOKEN": "t", "MY_SECRET": "m",
        "LLM_API_KEY": "l", "DB_PASSWORD": "d", "DATABASE_URL": "postgres://x", "AWS_REGION": "r",
    })
    assert env == {"PATH": "p", "APPDATA": "a", "USERPROFILE": "u"}


def test_opencode_runs_in_the_worktree_without_secrets(repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak")
    seen = {}

    def runner(command, **kwargs):
        if command[0] == "opencode":
            seen.update(command=command, cwd=kwargs["cwd"], env=kwargs["env"], timeout=kwargs["timeout"])
            Path(kwargs["cwd"], "feature.py").write_text("print('built')\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "done", "")
        return subprocess.run(command, check=False, **kwargs)

    ws = workspace(tmp_path / 'wt', runner=runner)
    path, _ = ws.create(repo, "run-2")
    brief = tmp_path / "brief.md"
    brief.write_text("Build it.", encoding="utf-8")

    result = ws.run_opencode(path, brief, "backend round 1")

    command = seen["command"]
    assert command[:3] == ["opencode", "run", OPENCODE_MESSAGE]
    assert command.index(OPENCODE_MESSAGE) < command.index("-f"), "-f would swallow the message"
    assert command[command.index("-m") + 1] == "opencode/test-model"
    assert command[-1] == str(brief.resolve())
    assert Path(seen["cwd"]) == path
    assert "OPENROUTER_API_KEY" not in seen["env"]
    assert seen["timeout"] == 60
    assert result.ok and (path / "feature.py").exists()


def test_changed_paths_and_diff_include_new_and_modified_files(repo: Path, tmp_path: Path):
    ws = workspace(tmp_path / 'wt')
    path, _ = ws.create(repo, "run-3")
    (path / "app.py").write_text("def hello():\n    return 'hello'\n", encoding="utf-8")
    (path / "tests").mkdir()
    (path / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    assert ws.changed_paths(path) == {"app.py", "tests/test_app.py"}
    diff = ws.diff(path)
    assert "return 'hello'" in diff
    assert "def test_x" in diff, "new files are part of the diff"
    assert "2 files changed" in diff


def test_diff_is_cut_with_a_note(repo: Path, tmp_path: Path):
    ws = workspace(tmp_path / 'wt', diff_max_chars=200)
    path, _ = ws.create(repo, "run-4")
    (path / "big.py").write_text("x = 1\n" * 500, encoding="utf-8")

    diff = ws.diff(path)
    assert len(diff) < 300
    assert "[diff cut:" in diff


def test_run_tests_reports_the_real_exit_code(repo: Path, tmp_path: Path):
    path, _ = workspace(tmp_path / 'wt').create(repo, "run-5")

    passed = workspace(tmp_path / 'wt').run_tests(path)
    failed = workspace(tmp_path / 'wt', test_command=f'"{sys.executable}" -c "import sys; print(\'boom\'); sys.exit(3)"').run_tests(path)

    assert passed.ok
    assert failed.exit_code == 3 and not failed.ok and "boom" in failed.output


def test_a_command_that_hangs_times_out(tmp_path: Path):
    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    result = workspace(tmp_path / 'wt', runner=runner).run_tests(tmp_path)
    assert result.timed_out and not result.ok


def test_is_test_path():
    assert is_test_path("tests/test_api.py")
    assert is_test_path("src/pkg/tests/helpers.py")
    assert is_test_path("test_app.py")
    assert is_test_path("app_test.py")
    assert not is_test_path("src/app.py")
    assert not is_test_path("contest.py")


def test_refuses_a_worktree_folder_inside_a_repository(repo: Path, tmp_path: Path):
    # Seen live: nested in another repository, opencode adopted that repository
    # as its workspace and wrote into it.
    with pytest.raises(WorkspaceError, match="inside the git repository"):
        workspace(repo / "nested").create(repo, "x")


def test_caches_are_not_changes(repo: Path, tmp_path: Path):
    ws = workspace(tmp_path / "wt")
    path, _ = ws.create(repo, "run-6")
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00")
    (path / ".pytest_cache").mkdir()
    (path / ".pytest_cache" / "README.md").write_text("cache", encoding="utf-8")
    (path / "real.py").write_text("x = 1\n", encoding="utf-8")

    assert ws.changed_paths(path) == {"real.py"}
    diff = ws.diff(path)
    assert "real.py" in diff and "__pycache__" not in diff and ".pytest_cache" not in diff


def test_watch_sees_a_write_outside_the_worktree(repo: Path, tmp_path: Path):
    ws = workspace(tmp_path / "wt")
    before = ws.watch([repo])
    (repo / "escaped.py").write_text("oops\n", encoding="utf-8")
    after = ws.watch([repo])

    assert "escaped.py" in after[str(repo)] and "escaped.py" not in before[str(repo)]


def test_commands_run_without_writing_bytecode(tmp_path: Path):
    seen = {}

    def runner(command, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, "", "")

    workspace(tmp_path / "wt", runner=runner).run_tests(tmp_path)
    assert seen["PYTHONDONTWRITEBYTECODE"] == "1"


def test_pwd_always_names_the_directory_the_command_runs_in(tmp_path: Path, monkeypatch):
    # Reproduced live: subprocess keeps the parent's PWD, opencode trusts PWD, and
    # with PWD pointing at the agent project it wrote there instead of the worktree.
    monkeypatch.setenv("PWD", "C:/somewhere/else")
    monkeypatch.setenv("OLDPWD", "C:/older")
    seen = {}

    def runner(command, **kwargs):
        seen.update(env=kwargs["env"], cwd=kwargs["cwd"])
        return subprocess.CompletedProcess(command, 0, "", "")

    target = tmp_path / "worktree"
    target.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("x", encoding="utf-8")
    workspace(tmp_path / "wt", runner=runner).run_opencode(target, brief, "t")

    assert seen["env"]["PWD"] == str(target.resolve())
    assert "OLDPWD" not in seen["env"]

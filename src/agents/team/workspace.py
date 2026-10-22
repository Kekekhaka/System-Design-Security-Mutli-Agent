"""The isolated build workspace: a git worktree on its own branch, opencode runs
inside it, and real test runs.

The engineers and QA work in a git worktree of the target repository, on a new
branch, so the operator's own checkout is never touched. A worktree holds only
tracked files, so untracked secrets such as `.env` are not in it. Secrets are
also stripped from the environment opencode and the tests run with. Nothing is
committed, pushed or merged here: the operator reviews the branch and decides.

The worktree must never sit inside another git repository. A worktree has a
`.git` *file*, and opencode finds its project by walking up to the nearest
`.git` *folder*: nested inside another repository, it adopts that repository
as its workspace (seen live: the engineer wrote into the agent project). So
worktrees live under TEAM_WORKTREE_ROOT, which is checked to be outside every
repository, and `watch()` lets the pipeline prove nothing was written elsewhere.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.core.config import settings

# Environment variables never passed to opencode or the tests.
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|AUTH", re.IGNORECASE)
_SECRET_PREFIXES = (
    "OPENROUTER_", "LANGFUSE_", "LLM_", "AWS_", "AZURE_", "GH_", "GITHUB_", "HF_",
    "ANTHROPIC_", "OPENAI_", "WHATSAPP_", "JWT_", "DATABASE_URL",
)
# Tool caches are never part of a build's changes.
_CACHE_PARTS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
_CACHE_EXCLUDES = tuple(f":(exclude,glob)**/{part}/**" for part in _CACHE_PARTS) + (
    ":(exclude,glob)**/*.pyc",
)
OUTPUT_TAIL_CHARS = 4000
OPENCODE_MESSAGE = (
    "Do the task in the attached brief, in this repository. Follow every rule in it."
)


class WorkspaceError(RuntimeError):
    """The build workspace could not be created."""


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str  # the last OUTPUT_TAIL_CHARS of stdout and stderr
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_dict(self) -> dict:
        return {"exit_code": self.exit_code, "timed_out": self.timed_out, "output": self.output}


def safe_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without anything that looks like a secret."""
    source = dict(os.environ if environ is None else environ)
    return {
        name: value
        for name, value in source.items()
        if not _SECRET_NAME.search(name) and not name.upper().startswith(_SECRET_PREFIXES)
    }


def is_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = parts[-1] if parts else ""
    return (
        any(part in ("tests", "test") for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def is_cache_path(path: str) -> bool:
    parts = Path(path).parts
    return path.endswith(".pyc") or any(part in _CACHE_PARTS for part in parts)


def default_worktree_root() -> Path:
    return Path(settings.TEAM_WORKTREE_ROOT) if settings.TEAM_WORKTREE_ROOT else (
        Path(tempfile.gettempdir()) / "team-worktrees"
    )


def _same_path(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _tail(text: str) -> str:
    return text[-OUTPUT_TAIL_CHARS:]


class GitWorkspace:
    def __init__(
        self,
        opencode_bin: str,
        opencode_model: str,
        engineer_timeout: int,
        test_command: str,
        test_timeout: int,
        diff_max_chars: int,
        worktree_root: Path | None = None,
        runner=subprocess.run,
    ) -> None:
        self.opencode_bin = opencode_bin
        self.opencode_model = opencode_model
        self.engineer_timeout = engineer_timeout
        self.test_command = test_command
        self.test_timeout = test_timeout
        self.diff_max_chars = diff_max_chars
        self.worktree_root = Path(worktree_root) if worktree_root else default_worktree_root()
        self.runner = runner

    @classmethod
    def from_settings(cls, runner=subprocess.run) -> GitWorkspace:
        return cls(
            settings.OPENCODE_BIN,
            settings.OPENCODE_MODEL,
            settings.ENGINEER_TIMEOUT_SECONDS,
            settings.TEAM_TEST_COMMAND,
            settings.TEAM_TEST_TIMEOUT_SECONDS,
            settings.BUILD_DIFF_MAX_CHARS,
            worktree_root=default_worktree_root(),
            runner=runner,
        )

    def _env(self, cwd: Path | None = None) -> dict[str, str]:
        env = safe_env()
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # keep bytecode out of the build's changes
        # subprocess changes the real working directory but keeps the parent's PWD,
        # and opencode trusts PWD over its real cwd (reproduced: with PWD pointing at
        # the agent project, opencode wrote there although it was started in the
        # worktree). So PWD always names the directory the command runs in.
        env.pop("OLDPWD", None)
        if cwd is not None:
            env["PWD"] = str(Path(cwd).resolve())
        else:
            env.pop("PWD", None)
        return env

    def _run(self, command: list[str] | str, cwd: Path, timeout: int | None = 120) -> CommandResult:
        try:
            completed = self.runner(
                command,
                cwd=str(cwd),
                env=self._env(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = f"{exc.stdout or ''}{exc.stderr or ''}"
            return CommandResult(-1, _tail(output if isinstance(output, str) else ""), timed_out=True)
        except FileNotFoundError:
            name = command if isinstance(command, str) else command[0]
            return CommandResult(127, f"command not found: {name}")
        return CommandResult(completed.returncode, _tail(f"{completed.stdout or ''}{completed.stderr or ''}"))

    def _git(self, cwd: Path, *args: str) -> CommandResult:
        return self._run(["git", *args], cwd)

    def _capture(self, cwd: Path, *git_args: str) -> str:
        """The complete stdout of a git command (not a tail), or "" on failure."""
        try:
            completed = self.runner(
                ["git", *git_args], cwd=str(cwd), env=self._env(cwd), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=120,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""
        return (completed.stdout or "") if completed.returncode == 0 else ""

    def create(self, repo: Path, run_id: str) -> tuple[Path, str]:
        """Add a worktree of `repo` at HEAD on a new branch; return (path, branch)."""
        repo = Path(repo).resolve()
        if not self._git(repo, "rev-parse", "--show-toplevel").ok:
            raise WorkspaceError(f"{repo} is not a git repository")
        if not self._git(repo, "rev-parse", "--verify", "HEAD").ok:
            raise WorkspaceError(f"{repo} has no commits to branch from")

        root = self.worktree_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        enclosing = self._capture(root, "rev-parse", "--show-toplevel").strip()
        if enclosing:
            raise WorkspaceError(
                f"the worktree folder {root} is inside the git repository {enclosing}; opencode "
                "would treat that repository as its workspace. Set TEAM_WORKTREE_ROOT to a "
                "folder outside every repository."
            )

        branch = f"team/{run_id}"
        path = root / f"{repo.name}-{run_id}"
        result = self._git(repo, "worktree", "add", "-b", branch, str(path), "HEAD")
        if not result.ok:
            raise WorkspaceError(f"git worktree add failed: {result.output.strip()[:300]}")
        toplevel = self._capture(path, "rev-parse", "--show-toplevel").strip()
        if not toplevel or not _same_path(toplevel, path):
            raise WorkspaceError(f"the worktree at {path} does not resolve to itself (git says {toplevel!r})")
        return path, branch

    def run_opencode(self, worktree: Path, brief: Path, title: str) -> CommandResult:
        # The message goes before -f: -f takes several files and would swallow it.
        command = [
            self.opencode_bin, "run", OPENCODE_MESSAGE,
            "-m", self.opencode_model, "--title", title, "-f", str(Path(brief).resolve()),
        ]
        return self._run(command, worktree, timeout=self.engineer_timeout)

    def changed_paths(self, worktree: Path) -> set[str]:
        paths = set()
        for line in self._capture(worktree, "status", "--porcelain=v1", "-uall").splitlines():
            entry = line[3:].strip()
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            entry = entry.strip('"')
            if entry and not is_cache_path(entry):
                paths.add(entry)
        return paths

    def snapshot(self, worktree: Path) -> dict[str, str]:
        """Content hash of every changed path, to see exactly what a step touched."""
        hashes = {}
        for path in self.changed_paths(worktree):
            file = Path(worktree) / path
            hashes[path] = hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else "deleted"
        return hashes

    def watch(self, repos: list[Path]) -> dict[str, str]:
        """`git status` of each repository outside the worktree, to prove nothing wrote there."""
        return {str(repo): self._capture(Path(repo), "status", "--porcelain=v1", "-uall") for repo in repos}

    def diff(self, worktree: Path) -> str:
        """Every change against HEAD, new files included, caches excluded, cut to size."""
        # Intent-to-add makes new files show up in `git diff` without staging them.
        self._git(worktree, "add", "--intent-to-add", "--all", "--", ".", *_CACHE_EXCLUDES)
        stat = self._capture(worktree, "diff", "HEAD", "--stat", "--", ".", *_CACHE_EXCLUDES).strip()
        body = self._capture(worktree, "diff", "HEAD", "--", ".", *_CACHE_EXCLUDES)
        combined = f"{stat}\n\n{body}" if stat else body
        if len(combined) > self.diff_max_chars:
            cut = len(combined) - self.diff_max_chars
            combined = combined[: self.diff_max_chars] + f"\n[diff cut: {cut} more characters not shown]"
        return combined

    def run_tests(self, worktree: Path) -> CommandResult:
        # Windows parses a command line itself (quoted paths included); shlex would
        # keep the quotes. Elsewhere, split it. No shell either way.
        command = self.test_command if os.name == "nt" else shlex.split(self.test_command)
        return self._run(command, worktree, timeout=self.test_timeout)

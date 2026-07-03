"""Worktree lifecycle helpers — owns the canonical worktree path layout.

All worktree paths used by the iAR agent runner flow through this module
so the ``create`` and ``path`` shell commands cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from backend.infrastructure.process_runner import CommandResult, SubprocessRunner


WORKTREE_DIR_NAME = ".iar-worktrees"


class WorktreeProcessRunner(Protocol):
    """Minimal subprocess contract used by :class:`WorktreeManager`.

    Matches the surface of ``SubprocessRunner`` so production code can pass
    an instance directly while tests can supply a stub.
    """

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        check: bool = True,
    ) -> CommandResult: ...


class WorktreeManager:
    """Create, locate, and remove iAR-owned Git worktrees.

    The path layout is fixed: every worktree for a given branch lives at
    ``{repo_root}/.iar-worktrees/{branch}``. Both ``create`` and ``path``
    resolve to this single layout, eliminating the historical class of bugs
    where three independent shell strings could disagree on the location.
    """

    def __init__(
        self,
        repo_root_path: Path,
        process_runner: WorktreeProcessRunner | None = None,
    ) -> None:
        """Initialize the manager.

        Args:
            repo_root_path: Absolute path to the target Git repository root.
            process_runner: Optional subprocess runner. Defaults to
                :class:`SubprocessRunner` so production code can construct
                the manager without dependencies; tests may inject a stub.
        """
        self._repo_root_path = repo_root_path
        self._process_runner: WorktreeProcessRunner = (
            process_runner if process_runner is not None else SubprocessRunner()
        )

    @property
    def repo_root_path(self) -> Path:
        """Return the repository root this manager operates on."""
        return self._repo_root_path

    @property
    def worktree_root(self) -> Path:
        """Return the directory that holds all managed worktrees."""
        return self._repo_root_path / WORKTREE_DIR_NAME

    def worktree_path(self, branch: str) -> Path:
        """Return the absolute path for ``branch`` (no side effects).

        Args:
            branch: Branch name (and worktree directory name).

        Returns:
            Absolute path to the worktree directory.
        """
        return self.worktree_root / branch

    def create(self, branch: str, base_branch: str) -> Path:
        """Create a worktree for ``branch`` based on ``base_branch``.

        Args:
            branch: New branch name to create.
            base_branch: Existing branch to fork from.

        Returns:
            Absolute path to the newly created worktree.

        Raises:
            subprocess.CalledProcessError: If ``git worktree add`` exits
                non-zero. The exception propagates with full stdout/stderr.
        """
        target_path = self.worktree_path(branch)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self._ensure_worktree_dir_excluded()
        self._process_runner.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch,
                str(target_path),
                base_branch,
            ],
            cwd=self._repo_root_path,
            check=True,
        )
        return target_path

    def _ensure_worktree_dir_excluded(self) -> None:
        """Keep the worktree directory out of the parent repository's index.

        Worktrees live inside the repository, so a repository-wide
        ``git add -A`` (commit proxies, quality hooks) would otherwise stage
        them as embedded-repository gitlinks. Writing ``info/exclude`` keeps
        the exclusion local instead of forcing a tracked ``.gitignore`` change
        on every target repository. Best effort: failures must never block
        worktree creation.
        """
        exclude_line = f"/{WORKTREE_DIR_NAME}/"
        rev_parse_result = self._process_runner.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=self._repo_root_path,
            check=False,
        )
        exclude_path_text = rev_parse_result.stdout.strip()
        if rev_parse_result.return_code != 0 or not exclude_path_text:
            return
        exclude_path = Path(exclude_path_text)
        if not exclude_path.is_absolute():
            exclude_path = self._repo_root_path / exclude_path
        try:
            existing_text = (
                exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
            )
            if exclude_line in existing_text.splitlines():
                return
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            separator = "" if not existing_text or existing_text.endswith("\n") else "\n"
            exclude_path.write_text(f"{existing_text}{separator}{exclude_line}\n", encoding="utf-8")
        except OSError:
            return

    def remove(self, branch: str) -> None:
        """Remove the worktree for ``branch`` and prune Git's metadata.

        The worktree is force-removed even if the working tree is dirty;
        ``git worktree prune`` then clears the stale administrative entry.

        Args:
            branch: Branch name whose worktree should be removed.

        Raises:
            subprocess.CalledProcessError: If either ``git worktree remove``
                or ``git worktree prune`` exits non-zero. The exception
                propagates with full stdout/stderr.
        """
        target_path = self.worktree_path(branch)
        self._process_runner.run(
            ["git", "worktree", "remove", "--force", str(target_path)],
            cwd=self._repo_root_path,
            check=True,
        )
        self._process_runner.run(
            ["git", "worktree", "prune"],
            cwd=self._repo_root_path,
            check=True,
        )

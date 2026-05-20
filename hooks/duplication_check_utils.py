#!/usr/bin/env python3
"""Shared helpers for duplication-prevention hook wrappers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable, Sequence


def repo_root_from_hook(script_file: Path) -> Path:
    """Return the repository root for a hook script file.

    Args:
        script_file: Path to the hook script file.

    Returns:
        Repository root directory path.
    """

    return script_file.resolve().parents[1]


def path_is_within_any_root(relative_path: Path, root_paths: Sequence[Path]) -> bool:
    """Return whether a relative path is inside one of the provided roots.

    Args:
        relative_path: Repository-relative path to check.
        root_paths: Repository-relative root paths.

    Returns:
        True when the path is equal to or nested under one root.
    """

    for root_path in root_paths:
        if relative_path == root_path:
            return True
        if relative_path.is_relative_to(root_path):
            return True
    return False


def _normalize_repo_relative_path(
    raw_path_text: str | Path, repo_root: Path
) -> Path | None:
    """Convert a filesystem path into a repository-relative path."""

    raw_path = Path(raw_path_text)
    absolute_path = raw_path if raw_path.is_absolute() else repo_root / raw_path
    try:
        return absolute_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None


def _expand_explicit_paths(
    raw_path_texts: Iterable[str],
    repo_root: Path,
    allowed_suffixes: set[str],
    allowed_root_paths: Sequence[Path],
) -> list[Path]:
    """Expand explicit file or directory inputs into repository-relative files."""

    selected_paths: list[Path] = []
    seen_relative_paths: set[Path] = set()

    for raw_path_text in raw_path_texts:
        relative_path = _normalize_repo_relative_path(raw_path_text, repo_root)
        if relative_path is None:
            continue

        absolute_path = repo_root / relative_path
        candidate_paths: list[Path]
        if absolute_path.is_dir():
            candidate_paths = [
                nested_path.relative_to(repo_root)
                for nested_path in sorted(absolute_path.rglob("*"))
                if nested_path.is_file()
            ]
        elif absolute_path.is_file():
            candidate_paths = [relative_path]
        else:
            continue

        for candidate_relative_path in candidate_paths:
            if candidate_relative_path.suffix not in allowed_suffixes:
                continue
            if allowed_root_paths and not path_is_within_any_root(
                candidate_relative_path, allowed_root_paths
            ):
                continue
            if candidate_relative_path in seen_relative_paths:
                continue
            seen_relative_paths.add(candidate_relative_path)
            selected_paths.append(candidate_relative_path)

    return selected_paths


def _git_relative_paths(repo_root: Path, git_args: Sequence[str]) -> list[Path]:
    """Return repository-relative paths from a git path listing command."""

    git_process = subprocess.run(
        ["git", *git_args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if git_process.returncode != 0:
        return []

    repository_relative_paths: list[Path] = []
    for raw_path_text in git_process.stdout.splitlines():
        stripped_path_text = raw_path_text.strip()
        if not stripped_path_text:
            continue
        repository_relative_paths.append(Path(stripped_path_text))
    return repository_relative_paths


def _collect_changed_paths(
    repo_root: Path,
    allowed_suffixes: set[str],
    allowed_root_paths: Sequence[Path],
) -> list[Path]:
    """Collect paths changed relative to HEAD, with a staged fallback."""

    changed_paths = _git_relative_paths(
        repo_root,
        [
            "diff",
            "--name-only",
            "HEAD",
            "--",
            *[root.as_posix() for root in allowed_root_paths],
        ],
    )
    if not changed_paths:
        changed_paths = _git_relative_paths(
            repo_root,
            [
                "diff",
                "--name-only",
                "--cached",
                "--diff-filter=ACMR",
                "--",
                *[root.as_posix() for root in allowed_root_paths],
            ],
        )

    selected_changed_paths: list[Path] = []
    seen_relative_paths: set[Path] = set()
    for candidate_relative_path in changed_paths:
        if candidate_relative_path.suffix not in allowed_suffixes:
            continue
        if allowed_root_paths and not path_is_within_any_root(
            candidate_relative_path, allowed_root_paths
        ):
            continue
        if candidate_relative_path in seen_relative_paths:
            continue
        seen_relative_paths.add(candidate_relative_path)
        selected_changed_paths.append(candidate_relative_path)

    return selected_changed_paths


def _collect_tracked_paths(
    repo_root: Path,
    allowed_suffixes: set[str],
    allowed_root_paths: Sequence[Path],
) -> set[Path]:
    """Collect tracked paths that can participate in duplication checks."""

    git_args = ["ls-files"]
    if allowed_root_paths:
        git_args.extend(["--", *[root.as_posix() for root in allowed_root_paths]])

    tracked_paths = _git_relative_paths(repo_root, git_args)
    selected_tracked_paths: set[Path] = set()
    for candidate_relative_path in tracked_paths:
        if candidate_relative_path.suffix not in allowed_suffixes:
            continue
        if allowed_root_paths and not path_is_within_any_root(
            candidate_relative_path, allowed_root_paths
        ):
            continue
        selected_tracked_paths.add(candidate_relative_path)

    return selected_tracked_paths


def select_incremental_paths(
    raw_path_texts: Sequence[str],
    repo_root: Path,
    allowed_suffixes: set[str],
    allowed_root_paths: Sequence[Path],
) -> list[Path]:
    """Return the minimal candidate set for an incremental duplication check.

    Args:
        raw_path_texts: Raw paths supplied by pre-commit or a manual invocation.
        repo_root: Repository root path.
        allowed_suffixes: File suffixes that the hook should consider.
        allowed_root_paths: Repository-relative roots the hook should consider.

    Returns:
        Repository-relative candidate paths. When a hook is invoked with a
        repository-wide file list on a clean tree, this returns an empty list so
        historical duplication does not block a fresh lint run.
        Set DUPLICATION_CHECK_FORCE=1 to force explicit tracked-file scans.
    """

    explicit_paths = _expand_explicit_paths(
        raw_path_texts=raw_path_texts,
        repo_root=repo_root,
        allowed_suffixes=allowed_suffixes,
        allowed_root_paths=allowed_root_paths,
    )
    changed_paths = _collect_changed_paths(
        repo_root=repo_root,
        allowed_suffixes=allowed_suffixes,
        allowed_root_paths=allowed_root_paths,
    )
    if changed_paths:
        changed_path_set = set(changed_paths)
        intersected_paths = [
            candidate_path
            for candidate_path in explicit_paths
            if candidate_path in changed_path_set
        ]
        if intersected_paths:
            return intersected_paths
        if not explicit_paths:
            return changed_paths

    if explicit_paths and os.environ.get("DUPLICATION_CHECK_FORCE") != "1":
        tracked_paths = _collect_tracked_paths(
            repo_root=repo_root,
            allowed_suffixes=allowed_suffixes,
            allowed_root_paths=allowed_root_paths,
        )
        if set(explicit_paths).issubset(tracked_paths):
            return []

    if explicit_paths:
        return explicit_paths
    return changed_paths

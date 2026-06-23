"""CLI helpers for PRD expansion and publishing.

Keeps ``backend.api.cli`` compact by isolating PRD-path handling and the
interactive publish prompt used by ``iar issue create``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from backend.core.use_cases.create_issue_from_prd import (
    ISSUE_LINK_LINE_RE,
    PrdPublishContext,
    current_git_branch,
    parse_issue_number,
    publish_prd_file,
)

if TYPE_CHECKING:
    from backend.core.shared.interfaces.agent_runner import (
        IGitHubClient,
        IProcessRunner,
    )
    from backend.core.shared.models.agent_runner import LabelConfig


def _prompt_and_publish_prd_if_needed(
    *,
    repo_path: Path,
    relative_prd_path: Path,
    issue_url: str,
    queue_ready: bool,
    git_remote: str,
    labels_config: "LabelConfig",
    github_client: "IGitHubClient",
    process_runner: "IProcessRunner",
) -> bool:
    """Prompt user to commit and push PRD changes if working tree is dirty."""

    status_result = process_runner.run(["git", "status", "--porcelain"], cwd=repo_path)
    if not status_result.stdout.strip():
        return False

    prd_path_text = relative_prd_path.as_posix()
    print(f"\n检测到 PRD 文件有未提交的变更：{prd_path_text}")
    response = input("是否立即 commit 并 push 该变更？(y/N): ")
    if response.lower() not in ("y", "yes"):
        return False

    current_branch = current_git_branch(repo_path, process_runner)
    publish_context = PrdPublishContext(
        repo_path=repo_path,
        relative_prd_path=relative_prd_path,
        git_remote=git_remote,
        current_branch=current_branch,
    )
    publish_prd_file(publish_context, process_runner)
    if queue_ready:
        github_client.edit_issue_labels(
            parse_issue_number(issue_url),
            add=[labels_config.ready],
        )
    return True


def _expand_prd_paths(
    repo_path: Path, prd_paths: list[str]
) -> tuple[list[str], list[str]]:
    """Expand directories in ``prd_paths`` to their ``*.md`` files.

    Files are returned as repo-relative paths. Directories are expanded to
    their immediate ``*.md`` children, sorted by filename. PRDs that already
    contain a ``- GitHub Issue:`` URL are skipped when discovered via a
    directory, because the user's intent is to create Issues only for pending
    PRDs. Explicitly passed files are not skipped so that errors remain
    visible. Non-existent paths are passed through unchanged so that downstream
    validation can report them with its usual diagnostics.

    Args:
        repo_path: Repository root used to resolve relative paths.
        prd_paths: Raw CLI arguments, each may be a file or a directory.

    Returns:
        ``(expanded_paths, skipped_paths)`` tuple. ``expanded_paths`` are
        repo-relative PRD Markdown files to process. ``skipped_paths`` are
        repo-relative PRD files that already have an Issue link and were
        discovered through a directory argument.

    Raises:
        ValueError: When a directory is empty of ``*.md`` files or the
            final expanded list is empty.
    """

    expanded_paths: list[str] = []
    skipped_paths: list[str] = []
    seen_paths: set[str] = set()

    def _has_issue_link(absolute_prd_path: Path) -> bool:
        try:
            prd_text = absolute_prd_path.read_text(encoding="utf-8")
        except OSError:
            return False
        return any(ISSUE_LINK_LINE_RE.match(line) for line in prd_text.splitlines())

    for prd_path_text in prd_paths:
        candidate_path = (repo_path / prd_path_text).resolve()

        if not candidate_path.exists():
            expanded_paths.append(prd_path_text)
            continue

        is_directory = candidate_path.is_dir()
        if candidate_path.is_file():
            if candidate_path.suffix.lower() != ".md":
                raise ValueError(f"PRD file must be a Markdown file: {prd_path_text}")
            file_entries = [candidate_path]
        elif is_directory:
            file_entries = sorted(
                [
                    entry
                    for entry in candidate_path.iterdir()
                    if entry.is_file() and entry.suffix.lower() == ".md"
                ],
                key=lambda entry: entry.name,
            )
            if not file_entries:
                raise ValueError(
                    f"Directory contains no PRD Markdown files: {prd_path_text}"
                )
        else:
            raise ValueError(
                f"PRD path is neither a file nor a directory: {prd_path_text}"
            )

        for file_entry in file_entries:
            relative_path = file_entry.relative_to(repo_path.resolve()).as_posix()
            if relative_path in seen_paths:
                continue
            seen_paths.add(relative_path)

            if is_directory and _has_issue_link(file_entry):
                skipped_paths.append(relative_path)
                continue

            expanded_paths.append(relative_path)

    if not expanded_paths and not skipped_paths:
        raise ValueError("No PRD Markdown files found.")

    return expanded_paths, skipped_paths

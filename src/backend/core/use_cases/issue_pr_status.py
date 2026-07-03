"""``iar issue list`` use case.

Assembles Issue + linked Pull Request views for the ``iar issue list``
command. Target repositories are resolved through
``resolve_repository_targets`` with a thin cwd auto-detect wrapper so the
single-repo / multi-repo decision can be made without copying the helper.

Architecture:

- This module owns cwd auto-detection, target resolution orchestration,
  per-repo error isolation, and the filter / sort / truncate pipeline.
- Per-issue PR fetching lives behind ``IGitHubClient`` so tests can swap
  a fake client without touching ``gh``.
- Rendering lives in ``backend.api.cli_typer``; this module only returns
  data.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backend.core.shared.interfaces.agent_runner import IGitHubClient
from backend.core.shared.models.agent_runner import (
    IssueSummary,
    IssueWithPulls,
    PullRequestSummary,
)

_logger = logging.getLogger(__name__)

# Tracks repositories that have already produced a "missing github_repo"
# warning during this process, so the stderr message is emitted at most
# once per repo per command invocation (matching the PRD's
# per-repo-not-per-issue requirement).
_warned_missing_github_repo: set[str] = set()

# Default number of issues to fetch per repository when ``--limit`` is
# not specified. Aligned with the PRD's ``--limit`` default of 100.
_DEFAULT_LIMIT = 100

# Issue states we accept on ``--state``.
_VALID_ISSUE_STATES = ("open", "closed", "all")

# File name whose presence at ``cwd`` signals an iAR project repo.
# Mirrors ``backend.infrastructure.config.settings.IAR_REPOSITORY_CONFIG_FILENAME``
# but is parameterised here so this use case stays free of any
# ``infrastructure``-layer import (architecture four-layer rule).
IAR_REPOSITORY_MARKER_FILENAME = ".iar.toml"


@dataclass(frozen=True)
class IssueListRequest:
    """Inputs for :func:`list_issues_with_prs`.

    Attributes:
        repo_id: Optional configured repository ID selector.
        repo_path_override: Optional ad-hoc repository path selector.
        all_repositories: Force multi-repository scan even when cwd is
            an iAR project repo.
        state_filter: GitHub issue state, one of ``"open"``,
            ``"closed"``, ``"all"``. Defaults to ``"all"``.
        label_filter: Optional label name to filter by.
        with_pr: ``True`` keeps only issues that have at least one PR,
            ``False`` keeps only PR-less issues, ``None`` applies no
            filter.
        limit: Maximum number of issues to fetch per repository.
    """

    repo_id: str | None = None
    repo_path_override: str | None = None
    all_repositories: bool = False
    state_filter: str = "all"
    label_filter: str | None = None
    with_pr: bool | None = None
    limit: int = _DEFAULT_LIMIT


@dataclass(frozen=True)
class IssueListResult:
    """Aggregated output of :func:`list_issues_with_prs`.

    Attributes:
        rows: Issue-with-PR view-model rows. ``repo`` is ``None`` for
            single-repository listings.
        errors: Per-repository error messages. Each tuple is
            ``(repo_label, error_message)``; the overall command exit
            code is non-zero when this is non-empty.
    """

    rows: tuple[IssueWithPulls, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()


def _has_local_iar_repo(cwd: Path) -> bool:
    """Return True when ``cwd`` contains an ``.iar.toml`` marker.

    Default predicate. The CLI dispatch may build a different
    predicate via :func:`make_default_has_local_iar_repo` to bind the
    marker name from the canonical infrastructure setting.
    """
    return (cwd / IAR_REPOSITORY_MARKER_FILENAME).is_file()


def make_default_has_local_iar_repo(
    marker_filename: str = IAR_REPOSITORY_MARKER_FILENAME,
) -> Callable[[Path], bool]:
    """Build a ``has_local_iar_repo`` predicate bound to a marker file name.

    The use case cannot import ``IAR_REPOSITORY_CONFIG_FILENAME`` from
    ``infrastructure`` (four-layer rule). Instead, the CLI dispatch
    layer builds the predicate via this factory so the single source of
    truth (``infrastructure.config.settings``) is still the place that
    knows the real filename.
    """

    def _predicate(cwd: Path) -> bool:
        return (cwd / marker_filename).is_file()

    return _predicate


class _RepoTargetResolver(Protocol):
    """Resolver contract injected into the use case.

    The concrete implementation is provided by the CLI dispatch layer,
    which closes over the real ``resolve_repository_targets`` engine
    helper (along with the loaded ``AgentRunnerSettings``). This keeps
    the use case free of ``engines``-layer imports while still letting
    it pick single-repo vs. multi-repo without copying the helper.
    """

    def __call__(
        self,
        *,
        repo_id: str | None,
        repo_path_override: str | None,
        all_repositories: bool,
    ) -> list: ...


def _resolve_targets_with_cwd_autodetect(
    request: IssueListRequest,
    *,
    cwd: Path,
    has_local_iar_repo: Callable[[Path], bool],
    resolve_targets: _RepoTargetResolver,
) -> list:
    """Apply cwd auto-detection on top of an injected target resolver.

    The wrapper exists so the use case can pick single-repo vs.
    all-registered without duplicating the helper's conflict-checking
    logic. Callers inject the helper to keep this module free of
    ``engines``-layer imports (matches the project four-layer rule:
    ``core`` cannot import ``engines``).

    Args:
        request: Validated caller request.
        cwd: Current working directory used for auto-detection.
        has_local_iar_repo: Predicate that returns ``True`` when ``cwd``
            looks like an iAR project repo (i.e. an ``.iar.toml`` file
            exists in ``cwd``).
        resolve_targets: Injected target resolver.

    Returns:
        list[RepositoryRunContext]: resolved repository contexts, ready
        for GitHub client construction.
    """
    if (
        request.repo_path_override is not None
        or request.repo_id is not None
        or request.all_repositories
    ):
        return resolve_targets(
            repo_id=request.repo_id,
            repo_path_override=request.repo_path_override,
            all_repositories=request.all_repositories,
        )
    if has_local_iar_repo(cwd):
        return resolve_targets(
            repo_id=None,
            repo_path_override=str(cwd),
            all_repositories=False,
        )
    return resolve_targets(repo_id=None, repo_path_override=None, all_repositories=True)


def _normalise_state_filter(state_filter: str) -> str:
    """Validate and normalise the ``--state`` value."""
    normalised = state_filter.strip().lower()
    if normalised not in _VALID_ISSUE_STATES:
        raise ValueError(
            f"Invalid --state {state_filter!r}; expected one of "
            f"{', '.join(_VALID_ISSUE_STATES)}."
        )
    return normalised


def _validate_request(request: IssueListRequest) -> str:
    """Validate the request once, returning the normalised state filter.

    Doing this eagerly means CLI dispatch can surface the failure
    directly via stderr before any GitHub call is attempted.
    """
    return _normalise_state_filter(request.state_filter)


def _repo_label_for(context) -> str | None:
    """Return the ``owner/name`` style label for a resolved context.

    The label is sourced exclusively from
    ``context.config.repositories[context.repo_id].github_repo`` — the
    identity populated by ``merge_repository_config`` from the
    user-configured ``github_repo`` field. Returns ``None`` when the
    identity is missing or the field is unset, which causes
    ``_build_issue_with_pulls`` to leave the PR column empty.
    """
    repo_id = getattr(context, "repo_id", None)
    config = getattr(context, "config", None)
    if repo_id is None or config is None:
        return None
    repositories = getattr(config, "repositories", None) or {}
    identity = repositories.get(repo_id)
    if identity is None:
        return None
    github_repo = getattr(identity, "github_repo", None)
    if not github_repo:
        return None
    return github_repo


def _warn_missing_github_repo(repo_id: str) -> None:
    """Emit a one-shot stderr warning when ``github_repo`` is unconfigured.

    Guarded by a module-level set so the message appears at most once
    per repo per process, satisfying the PRD's per-repo-not-per-issue
    requirement.
    """
    if repo_id in _warned_missing_github_repo:
        return
    _warned_missing_github_repo.add(repo_id)
    print(
        f"[WARN] Repository '{repo_id}' has no github_repo configured; "
        'PR column will be empty. Set github_repo = "owner/name" in '
        "[agent_runner.repositories.<id>] or .iar.toml.",
        file=sys.stderr,
    )


def _is_single_repo_mode(contexts: list) -> bool:
    """Return True when the listing collapses to a single repo."""
    return len(contexts) == 1


def _format_repo_error(context, exc: BaseException) -> str:
    """Build a per-repo error string with the repo path and reason."""
    repo_path = getattr(context, "repo_path", "<unknown>")
    return f"{repo_path}: {exc}"


def _build_issue_with_pulls(
    issue: IssueSummary,
    *,
    repo_label: str | None,
    github_client: IGitHubClient,
) -> IssueWithPulls:
    """Pull the PR list for one issue and assemble the view model."""
    if repo_label is None:
        pulls: tuple[PullRequestSummary, ...] = ()
    else:
        pulls = tuple(github_client.list_pull_requests_for_issue(repo_label, issue.number))
    return IssueWithPulls(
        repo=repo_label,
        number=issue.number,
        title=issue.title,
        state=issue.state,
        labels=issue.labels,
        updated_at="",
        url=issue.url,
        pulls=pulls,
    )


@dataclass(frozen=True)
class _RepoOutcome:
    """Per-repo aggregation kept before the global filter pass."""

    repo_label: str | None
    rows: tuple[IssueWithPulls, ...]
    error: str | None = None


def _process_one_repo(
    context,
    *,
    request: IssueListRequest,
    github_client_factory: Callable[[Path], IGitHubClient],
) -> _RepoOutcome:
    """Fetch issues + linked PRs for one repo, isolating failures."""
    repo_label = _repo_label_for(context)
    if repo_label is None:
        repo_id = getattr(context, "repo_id", "<unknown>")
        _warn_missing_github_repo(repo_id)
    try:
        github_client = github_client_factory(context.repo_path)
        issues = github_client.list_issues_by_label(
            label=request.label_filter,
            limit=request.limit,
            state=_normalise_state_filter(request.state_filter),
        )
    except Exception as exc:  # noqa: BLE001 - error isolation.
        return _RepoOutcome(
            repo_label=repo_label,
            rows=(),
            error=_format_repo_error(context, exc),
        )
    rows = tuple(
        _build_issue_with_pulls(issue, repo_label=repo_label, github_client=github_client)
        for issue in issues
    )
    return _RepoOutcome(repo_label=repo_label, rows=rows)


def _should_keep_issue(issue: IssueWithPulls, *, with_pr: bool | None) -> bool:
    """Apply the ``--with-pr`` / ``--without-pr`` filter."""
    if with_pr is None:
        return True
    has_prs = bool(issue.pulls)
    return has_prs if with_pr else not has_prs


def _sort_rows(rows: list[IssueWithPulls]) -> list[IssueWithPulls]:
    """Sort multi-repo results by ``repo`` asc, ``number`` asc.

    The PRD requires default sort by ``updated_at desc``; until GitHub
    exposes a stable ``updated_at`` for every Issue (the current
    ``IssueSummary`` does not), we fall back to ``number`` ascending as
    a stable, predictable order.
    """

    def _sort_key(row: IssueWithPulls) -> tuple[str, int]:
        repo_key = row.repo or ""
        return (repo_key, row.number)

    return sorted(rows, key=_sort_key)


def list_issues_with_prs(
    request: IssueListRequest,
    *,
    cwd: Path,
    github_client_factory: Callable[[Path], IGitHubClient],
    resolve_targets: _RepoTargetResolver,
    has_local_iar_repo: Callable[[Path], bool] = _has_local_iar_repo,
) -> IssueListResult:
    """Resolve, fetch, and assemble the issue + PR view-model rows.

    Args:
        request: Validated caller request.
        cwd: Current working directory used for the auto-detect wrapper.
        github_client_factory: Function that builds an
            ``IGitHubClient`` for a given repository path.
        resolve_targets: Injected helper bound to the engine's
            ``resolve_repository_targets`` factory; injected here so the
            use case stays in ``core/``.
        has_local_iar_repo: Predicate used by the cwd auto-detect
            wrapper; injected so tests can avoid touching disk.

    Returns:
        IssueListResult: rows + per-repo errors. Empty ``errors`` means
        the command should exit 0.
    """
    _validate_request(request)

    contexts = _resolve_targets_with_cwd_autodetect(
        request,
        cwd=cwd,
        has_local_iar_repo=has_local_iar_repo,
        resolve_targets=resolve_targets,
    )

    outcomes: list[_RepoOutcome] = []
    for context in contexts:
        outcomes.append(
            _process_one_repo(
                context,
                request=request,
                github_client_factory=github_client_factory,
            )
        )

    all_rows: list[IssueWithPulls] = []
    errors: list[tuple[str, str]] = []
    for outcome in outcomes:
        if outcome.error:
            label = outcome.repo_label or "<repo>"
            errors.append((label, outcome.error))
            continue
        all_rows.extend(outcome.rows)

    filtered = [row for row in all_rows if _should_keep_issue(row, with_pr=request.with_pr)]
    sorted_rows = _sort_rows(filtered)

    return IssueListResult(
        rows=tuple(sorted_rows),
        errors=tuple(errors),
    )


def render_pr_column(pulls: tuple[PullRequestSummary, ...]) -> str:
    """Format the PR column for ``--output table``.

    Empty pulls render as ``"—"`` to keep the column visually full even
    when an issue has no linked PRs. Multiple PRs are joined by
    ``", "``.
    """
    if not pulls:
        return "—"
    return ", ".join(f"#{pull.number} [{pull.state}]" for pull in pulls)


def render_issue_with_pulls_json(row: IssueWithPulls) -> dict:
    """Serialise a single row to a JSON-friendly dict."""
    return {
        "repo": row.repo,
        "number": row.number,
        "title": row.title,
        "state": row.state,
        "labels": list(row.labels),
        "updated_at": row.updated_at,
        "url": row.url,
        "pulls": [
            {
                "number": pull.number,
                "state": pull.state,
                "url": pull.url,
                "is_draft": pull.is_draft,
                "merged": pull.merged,
                "title": pull.title,
            }
            for pull in row.pulls
        ],
    }

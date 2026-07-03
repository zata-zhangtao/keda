"""Tests for ``iar issue list`` use case and dispatch wiring.

These tests pin down the use-case behavior and CLI dispatch logic in
``backend.api.cli``. The GitHub CLI implementation is exercised by the
shared ``FakeGitHubClient`` to keep the suite hermetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backend.api.cli_parser import build_parser
from backend.api.cli_typer import app
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    PullRequestSummary,
    RepositoryIdentity,
)
from backend.core.use_cases.issue_pr_status import (
    IssueListRequest,
    list_issues_with_prs,
    render_pr_column,
)
from tests.conftest import FakeGitHubClient


@dataclass(frozen=True)
class _FakeContext:
    """Minimal stand-in for ``RepositoryRunContext`` used in tests."""

    repo_id: str
    display_name: str
    repo_path: Path
    config: object = field(default=None)


def _make_context(
    repo_id: str,
    path: Path,
    *,
    github_repo: str | None = None,
) -> _FakeContext:
    """Build a context with a merged ``AppConfig`` that has a github_repo.

    When ``github_repo`` is ``None`` the identity is registered without
    one — the PR column then stays empty (this exercises the missing-
    config branch). When it is a string it is treated as the
    ``owner/name`` label that ``gh pr list --repo`` expects.
    """
    identity = RepositoryIdentity(
        id=repo_id,
        path=str(path),
        display_name=repo_id,
        github_repo=github_repo,
    )
    return _FakeContext(
        repo_id=repo_id,
        display_name=repo_id,
        repo_path=path,
        config=AppConfig(repositories={repo_id: identity}),
    )


def _make_issue(
    number: int, title: str, *, state: str = "OPEN", labels: tuple[str, ...] = ()
) -> IssueSummary:
    return IssueSummary(
        number=number,
        title=title,
        url=f"https://example.com/repo/issues/{number}",
        body="",
        labels=labels,
        state=state,
    )


def _make_pr(
    number: int,
    state: str,
    *,
    is_draft: bool = False,
    merged: bool = False,
    title: str = "",
) -> PullRequestSummary:
    return PullRequestSummary(
        number=number,
        state=state,
        url=f"https://example.com/repo/pull/{number}",
        is_draft=is_draft,
        merged=merged,
        title=title or f"PR #{number}",
    )


def _fake_resolve_factory(contexts):
    """Return a ``resolve_targets`` callable returning the given list."""

    def _resolve(*, repo_id, repo_path_override, all_repositories):
        return list(contexts)

    return _resolve


# ---------------------------------------------------------------------------
# cwd auto-detect wrapper
# ---------------------------------------------------------------------------


def test_resolve_targets_with_cwd_autodetect_single_repo() -> None:
    """cwd with .iar.toml → equivalent to --repo cwd."""
    requests_seen: list[dict[str, object]] = []

    def _fake_resolve(*, repo_id, repo_path_override, all_repositories):
        requests_seen.append(
            {
                "repo_id": repo_id,
                "repo_path_override": repo_path_override,
                "all_repositories": all_repositories,
            }
        )
        return [_make_context("test-repo", Path("/work"))]

    request = IssueListRequest()
    cwd = Path("/work")
    from backend.core.use_cases.issue_pr_status import (
        _resolve_targets_with_cwd_autodetect,
    )

    contexts = _resolve_targets_with_cwd_autodetect(
        request,
        cwd=cwd,
        has_local_iar_repo=lambda c: c == cwd,
        resolve_targets=_fake_resolve,
    )
    assert len(contexts) == 1
    assert requests_seen[0]["repo_path_override"] == str(cwd)
    assert requests_seen[0]["all_repositories"] is False


def test_resolve_targets_with_cwd_autodetect_all_registered() -> None:
    """cwd without .iar.toml → equivalent to --all-registered."""
    requests_seen: list[dict[str, object]] = []

    def _fake_resolve(*, repo_id, repo_path_override, all_repositories):
        requests_seen.append(
            {
                "repo_id": repo_id,
                "repo_path_override": repo_path_override,
                "all_repositories": all_repositories,
            }
        )
        return [_make_context("keda", Path("/repos/keda"))]

    request = IssueListRequest()
    from backend.core.use_cases.issue_pr_status import (
        _resolve_targets_with_cwd_autodetect,
    )

    _ = _resolve_targets_with_cwd_autodetect(
        request,
        cwd=Path("/tmp"),
        has_local_iar_repo=lambda _: False,
        resolve_targets=_fake_resolve,
    )
    assert requests_seen[0]["all_repositories"] is True
    assert requests_seen[0]["repo_path_override"] is None


def test_resolve_targets_with_cwd_autodetect_explicit_repo() -> None:
    """Explicit --repo bypasses auto-detect even when cwd has marker."""
    requests_seen: list[dict[str, object]] = []

    def _fake_resolve(*, repo_id, repo_path_override, all_repositories):
        requests_seen.append(
            {
                "repo_id": repo_id,
                "repo_path_override": repo_path_override,
                "all_repositories": all_repositories,
            }
        )
        return [_make_context("test-repo", Path("/other"))]

    request = IssueListRequest(repo_path_override="/other")
    from backend.core.use_cases.issue_pr_status import (
        _resolve_targets_with_cwd_autodetect,
    )

    _ = _resolve_targets_with_cwd_autodetect(
        request,
        cwd=Path("/work"),
        has_local_iar_repo=lambda _: True,
        resolve_targets=_fake_resolve,
    )
    assert requests_seen[0]["repo_path_override"] == "/other"
    assert requests_seen[0]["all_repositories"] is False


# ---------------------------------------------------------------------------
# End-to-end use case
# ---------------------------------------------------------------------------


def test_list_issues_with_prs_returns_rows(tmp_path: Path) -> None:
    """End-to-end use case: fetch issues + linked PRs for one repo."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    github_client = FakeGitHubClient()
    github_client.set_list_issues_by_label_result(
        [
            _make_issue(1, "Add list command"),
            _make_issue(2, "Fix label sync"),
        ]
    )
    github_client.set_prs_for_repo_issue(
        "owner/repo",
        1,
        [_make_pr(42, "merged", merged=True)],
    )
    github_client.set_prs_for_repo_issue("owner/repo", 2, [])

    result = list_issues_with_prs(
        IssueListRequest(),
        cwd=tmp_path,
        github_client_factory=lambda _: github_client,
        resolve_targets=_fake_resolve_factory(
            [_make_context("test-repo", repo_path, github_repo="owner/repo")]
        ),
        has_local_iar_repo=lambda _: True,
    )

    assert [row.number for row in result.rows] == [1, 2]
    assert result.rows[0].pulls[0].state == "merged"
    assert result.rows[1].pulls == ()
    assert result.errors == ()


def test_list_issues_with_prs_with_pr_filter(tmp_path: Path) -> None:
    """--with-pr keeps only issues with at least one PR."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    github_client = FakeGitHubClient()
    github_client.set_list_issues_by_label_result(
        [
            _make_issue(1, "Add list command"),
            _make_issue(2, "Fix label sync"),
            _make_issue(3, "Update docs"),
        ]
    )
    github_client.set_prs_for_repo_issue("owner/repo", 1, [_make_pr(42, "merged", merged=True)])
    github_client.set_prs_for_repo_issue("owner/repo", 2, [_make_pr(43, "draft", is_draft=True)])
    # issue 3 has no PRs

    result = list_issues_with_prs(
        IssueListRequest(with_pr=True),
        cwd=tmp_path,
        github_client_factory=lambda _: github_client,
        resolve_targets=_fake_resolve_factory(
            [_make_context("test-repo", repo_path, github_repo="owner/repo")]
        ),
        has_local_iar_repo=lambda _: True,
    )
    assert {row.number for row in result.rows} == {1, 2}


def test_list_issues_with_prs_without_pr_filter(tmp_path: Path) -> None:
    """--without-pr keeps only issues with no linked PRs."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    github_client = FakeGitHubClient()
    github_client.set_list_issues_by_label_result(
        [
            _make_issue(1, "Has PR"),
            _make_issue(2, "No PR"),
        ]
    )
    github_client.set_prs_for_repo_issue("owner/repo", 1, [_make_pr(42, "merged", merged=True)])

    result = list_issues_with_prs(
        IssueListRequest(with_pr=False),
        cwd=tmp_path,
        github_client_factory=lambda _: github_client,
        resolve_targets=_fake_resolve_factory(
            [_make_context("test-repo", repo_path, github_repo="owner/repo")]
        ),
        has_local_iar_repo=lambda _: True,
    )
    assert [row.number for row in result.rows] == [2]


def test_list_issues_with_prs_state_filter_is_validated(tmp_path: Path) -> None:
    """Invalid --state raises ValueError before any GitHub call."""
    github_client = FakeGitHubClient()

    with pytest.raises(ValueError, match="Invalid --state"):
        list_issues_with_prs(
            IssueListRequest(state_filter="garbage"),
            cwd=tmp_path,
            github_client_factory=lambda _: github_client,
            resolve_targets=_fake_resolve_factory([_make_context("test-repo", tmp_path)]),
            has_local_iar_repo=lambda _: True,
        )


def test_list_issues_with_prs_partial_failure_isolates_errors(tmp_path: Path) -> None:
    """A repo that raises must not stop other repos from rendering."""
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    good_client = FakeGitHubClient()
    good_client.set_list_issues_by_label_result([_make_issue(1, "From good repo")])

    def _factory(path: Path) -> FakeGitHubClient:
        if path == repo_a:
            raise RuntimeError("gh unavailable")
        return good_client

    result = list_issues_with_prs(
        IssueListRequest(),
        cwd=tmp_path,
        github_client_factory=_factory,
        resolve_targets=_fake_resolve_factory(
            [
                _make_context("repo-a", repo_a, github_repo="owner-a/repo-a"),
                _make_context("repo-b", repo_b, github_repo="owner-b/repo-b"),
            ]
        ),
        has_local_iar_repo=lambda _: False,
    )
    assert [row.number for row in result.rows] == [1]
    assert len(result.errors) == 1
    assert result.errors[0][0] == "owner-a/repo-a"
    assert "gh unavailable" in result.errors[0][1]


# ---------------------------------------------------------------------------
# PR column rendering
# ---------------------------------------------------------------------------


def test_render_pr_column_formats_pulls() -> None:
    """PR column renders `#N [state]` lists, joined by `, `."""
    pulls = (
        _make_pr(42, "merged", merged=True),
        _make_pr(43, "draft", is_draft=True),
    )
    assert render_pr_column(pulls) == "#42 [merged], #43 [draft]"
    assert render_pr_column(()) == "—"


# ---------------------------------------------------------------------------
# _repo_label_for: identity from AppConfig.repositories
# ---------------------------------------------------------------------------


def test_repo_label_for_returns_github_repo(tmp_path: Path) -> None:
    """Configured github_repo becomes the label passed to gh."""
    from backend.core.use_cases.issue_pr_status import _repo_label_for

    context = _make_context("keda", tmp_path, github_repo="zata-zhangtao/keda")
    assert _repo_label_for(context) == "zata-zhangtao/keda"


def test_repo_label_for_returns_none_when_github_repo_missing(tmp_path: Path) -> None:
    """Unconfigured github_repo yields None so the PR column stays empty."""
    from backend.core.use_cases.issue_pr_status import _repo_label_for

    context = _make_context("keda", tmp_path, github_repo=None)
    assert _repo_label_for(context) is None


def test_repo_label_for_handles_legacy_context_without_config(tmp_path: Path) -> None:
    """Context without a config (defensive) returns None instead of crashing."""
    from backend.core.use_cases.issue_pr_status import _repo_label_for

    legacy = _FakeContext(repo_id="keda", display_name="keda", repo_path=tmp_path, config=None)
    assert _repo_label_for(legacy) is None


def test_list_issues_with_prs_emits_warn_when_github_repo_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing github_repo prints a stderr WARN once and skips PR column."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    github_client = FakeGitHubClient()
    github_client.set_list_issues_by_label_result(
        [_make_issue(1, "No PR"), _make_issue(2, "Still no PR")]
    )

    result = list_issues_with_prs(
        IssueListRequest(),
        cwd=tmp_path,
        github_client_factory=lambda _: github_client,
        resolve_targets=_fake_resolve_factory(
            [_make_context("unconfigured", repo_path, github_repo=None)]
        ),
        has_local_iar_repo=lambda _: True,
    )
    assert [row.number for row in result.rows] == [1, 2]
    for row in result.rows:
        assert row.pulls == ()
        assert row.repo is None
    captured = capsys.readouterr()
    assert "has no github_repo configured" in captured.err
    # The warning is per-repo, not per-issue, even when multiple issues are listed.
    assert captured.err.count("has no github_repo configured") == 1


# ---------------------------------------------------------------------------
# CLI parser + Typer smoke tests
# ---------------------------------------------------------------------------


def test_argparse_parser_accepts_issue_list_command() -> None:
    """Argparse parser recognises `issue list` and the new flags."""
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--with-pr",
            "--limit",
            "20",
            "--output",
            "json",
        ]
    )
    assert parsed.command == "issue list"
    assert parsed.state == "open"
    assert parsed.with_pr is True
    assert parsed.limit == 20
    assert parsed.output == "json"


def test_argparse_parser_issue_list_defaults() -> None:
    """Defaults match the PRD: state=all, limit=100, output=table."""
    parser = build_parser()
    parsed = parser.parse_args(["issue", "list"])
    assert parsed.state == "all"
    assert parsed.with_pr is False
    assert parsed.without_pr is False
    assert parsed.limit == 100
    assert parsed.output == "table"
    assert parsed.all_registered is False


def test_typer_issue_list_help_renders() -> None:
    """Typer app exposes `iar issue list` and renders help text."""
    runner = CliRunner()
    result = runner.invoke(app, ["issue", "list", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "List Issues" in result.stdout

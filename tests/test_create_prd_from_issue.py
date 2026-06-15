"""Tests for create_prd_from_issue use case."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    LabelConfig,
)
from backend.core.use_cases.create_prd_from_issue import (
    CreatePrdFromIssueRequest,
    _build_fallback_prd,
    _generate_slug,
    _parse_prd_prefix,
    _resolve_prd_path,
    _update_issue_body_with_prd_path,
    create_prd_from_issue,
)
from tests.conftest import FakeGitHubClient


def _make_issue(
    number: int, title: str, body: str, labels: tuple[str, ...] = ()
) -> IssueSummary:
    return IssueSummary(
        number=number,
        title=title,
        url=f"https://github.com/example/repo/issues/{number}",
        body=body,
        labels=labels,
    )


def test_generate_slug_basic() -> None:
    """Slug should be lowercased, hyphenated and truncated."""
    assert _generate_slug("Hello World") == "hello-world"


def test_generate_slug_special_chars() -> None:
    """Special characters should be stripped."""
    assert _generate_slug("Feature: Add OAuth2!!!") == "feature-add-oauth2"


def test_generate_slug_long_title() -> None:
    """Slug should be truncated to 60 characters."""
    long_title = "a" * 100
    slug = _generate_slug(long_title)
    assert len(slug) <= 60
    assert slug == "a" * 60


def test_parse_prd_prefix_from_labels() -> None:
    """Prefix should be derived from priority/type labels."""
    issue = _make_issue(
        1,
        "Any title",
        "",
        labels=("priority/P1", "type/bug"),
    )
    assert _parse_prd_prefix(issue) == "P1-BUG"


def test_parse_prd_prefix_from_title_prefix() -> None:
    """Prefix should fall back to title prefix when type label is missing."""
    issue = _make_issue(1, "[Docs] Update guide", "")
    assert _parse_prd_prefix(issue) == "P2-DOCS"


def test_parse_prd_prefix_defaults() -> None:
    """Prefix should default to P2-FEAT when no hints are present."""
    issue = _make_issue(1, "Some feature", "")
    assert _parse_prd_prefix(issue) == "P2-FEAT"


def test_resolve_prd_path_new_issue(tmp_path: Path) -> None:
    """New issue without PRD path should generate a new pending path."""
    issue = _make_issue(1, "New Feature", "")
    path = _resolve_prd_path(repo_path=tmp_path, issue=issue)
    assert path.parent.name == "pending"
    assert path.name.startswith("P2-FEAT-20")
    assert "-prd-new-feature.md" in path.name


def test_resolve_prd_path_existing_prd(tmp_path: Path) -> None:
    """Issue with existing PRD path should reuse that path."""
    existing = tmp_path / "tasks" / "pending" / "existing.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("old", encoding="utf-8")
    issue = _make_issue(2, "Existing", "- PRD path: `tasks/pending/existing.md`")
    path = _resolve_prd_path(repo_path=tmp_path, issue=issue)
    assert path == existing


def test_update_issue_body_with_prd_path_insert() -> None:
    """Should insert PRD path at top when not present."""
    body = "Some description."
    updated = _update_issue_body_with_prd_path(body, "tasks/pending/foo.md")
    assert updated.startswith("- PRD path: `tasks/pending/foo.md`\n\n")
    assert "Some description." in updated


def test_update_issue_body_with_prd_path_replace() -> None:
    """Should replace existing canonical PRD path line."""
    body = "- PRD path: `tasks/pending/old.md`\n\nSome description."
    updated = _update_issue_body_with_prd_path(body, "tasks/pending/new.md")
    assert "- PRD path: `tasks/pending/new.md`" in updated
    assert "old.md" not in updated


def test_update_issue_body_with_prd_path_ignores_inline_mention() -> None:
    """Inline `PRD path:` in prose must not be rewritten."""
    body = (
        "Add a core workflow with an optional `PRD path:` anchor.\n\n"
        "- PRD path: `tasks/pending/old.md`\n"
    )
    updated = _update_issue_body_with_prd_path(body, "tasks/pending/new.md")
    assert "- PRD path: `tasks/pending/new.md`" in updated
    assert "optional `PRD path:` anchor" in updated


def test_build_fallback_prd_structure() -> None:
    """Fallback PRD should contain required sections."""
    issue = _make_issue(3, "Test", "Body text")
    prd = _build_fallback_prd(issue)
    assert prd.startswith("# PRD: Test")
    assert "- GitHub Issue:" in prd
    assert "## 1. Introduction & Goals" in prd
    assert "Body text" in prd
    assert "## 2. Requirement Shape" in prd
    assert "## 3. Acceptance Checklist" in prd


def test_create_prd_from_issue_new_prd(tmp_path: Path) -> None:
    """New issue should create a PRD file and update issue body/labels."""
    pending_dir = tmp_path / "tasks" / "pending"
    pending_dir.mkdir(parents=True)
    issue = _make_issue(
        4, "Generate PRD", "Need a feature.", labels=("agent/rework-prd",)
    )
    fake_client = FakeGitHubClient()
    fake_client.set_rework_prd_issues([issue])
    request = CreatePrdFromIssueRequest(
        repo_path=tmp_path,
        issue=issue,
        config=AppConfig(labels=LabelConfig(rework_prd="agent/rework-prd")),
        queue_ready=True,
    )
    prd_path = create_prd_from_issue(request=request, github_client=fake_client)

    assert prd_path.exists()
    assert prd_path.name.startswith("P2-FEAT-")
    assert "-prd-generate-prd.md" in prd_path.name
    assert prd_path.read_text(encoding="utf-8").startswith("# PRD: Generate PRD")
    body_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_body"]
    assert len(body_calls) == 1
    assert "PRD path:" in body_calls[0]["body"]
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 1
    assert "source/prd" in label_calls[0]["add"]
    assert "agent/rework-prd" in label_calls[0]["remove"]
    assert "agent/ready" in label_calls[0]["add"]
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "PRD generated successfully" in comment_calls[0]["body"]


def test_create_prd_from_issue_rewrite_existing_prd(tmp_path: Path) -> None:
    """Existing PRD should be rewritten in place."""
    pending_dir = tmp_path / "tasks" / "pending"
    pending_dir.mkdir(parents=True)
    existing_prd = pending_dir / "existing.md"
    existing_prd.write_text("# PRD: Old\n\n- GitHub Issue: #5\n", encoding="utf-8")
    issue = _make_issue(
        5,
        "Rewrite PRD",
        "- PRD path: `tasks/pending/existing.md`\n\nUpdated requirement.",
        labels=("agent/rework-prd",),
    )
    fake_client = FakeGitHubClient()
    fake_client._issue_comments[5] = ["New comment requirement."]
    request = CreatePrdFromIssueRequest(
        repo_path=tmp_path,
        issue=issue,
        config=AppConfig(labels=LabelConfig(rework_prd="agent/rework-prd")),
        queue_ready=False,
    )
    prd_path = create_prd_from_issue(request=request, github_client=fake_client)

    assert prd_path == existing_prd
    content = prd_path.read_text(encoding="utf-8")
    assert (
        "Updated requirement." in content
        or "New comment requirement." in content
        or "# PRD: Rewrite PRD" in content
    )
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert "agent/rework-prd" in label_calls[0]["remove"]
    assert "agent/ready" not in label_calls[0]["add"]


def test_create_prd_from_issue_failure_no_write(tmp_path: Path) -> None:
    """If write fails, exception should propagate so orchestrator can mark failed."""
    pending_dir = tmp_path / "tasks" / "pending"
    pending_dir.mkdir(parents=True)
    issue = _make_issue(6, "Fail", "", labels=("agent/rework-prd",))
    fake_client = FakeGitHubClient()

    original_write_text = Path.write_text

    def broken_write_text(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.endswith("-prd-fail.md"):
            raise RuntimeError("disk full")
        return original_write_text(self, *args, **kwargs)

    request = CreatePrdFromIssueRequest(
        repo_path=tmp_path,
        issue=issue,
        config=AppConfig(labels=LabelConfig(rework_prd="agent/rework-prd")),
    )
    from unittest.mock import patch

    with patch("pathlib.Path.write_text", broken_write_text):
        with pytest.raises(RuntimeError, match="disk full"):
            create_prd_from_issue(request=request, github_client=fake_client)

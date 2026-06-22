"""Tests for agent_runner_dependencies module."""

from __future__ import annotations

import pytest

from backend.core.shared.models.agent_runner import (
    DependencyBlocker,
    DependencyDeclaration,
    DependencyVerdict,
    DeliveryDependencyDeclaration,
    IssueSummary,
    LabelConfig,
)
from backend.core.use_cases.agent_runner_dependencies import (
    _canonical_blockers_hash,
    build_waiting_comment,
    clear_dependency_waiting,
    evaluate_dependencies,
    format_dependency_marker,
    format_dependency_wait_marker,
    mark_dependency_waiting,
    parse_delivery_dependencies,
    parse_dependency_marker,
    parse_latest_dependency_wait_marker,
)
from backend.core.use_cases.create_issue_from_prd import _resolve_dependencies


# ---------------------------------------------------------------------------
# parse_delivery_dependencies
# ---------------------------------------------------------------------------


class TestParseDeliveryDependencies:
    def test_empty_prd_returns_none(self) -> None:
        result = parse_delivery_dependencies("# PRD: Foo\n\nSome text.")
        assert result == DeliveryDependencyDeclaration()

    def test_parses_all_fields(self) -> None:
        prd_text = """
## Delivery Dependencies

- Group: my-group
- Depends on groups: group-a, group-b
- Depends on tasks/issues: #42, 43
- Gate type: hard
- Notes: wait for upstream
"""
        result = parse_delivery_dependencies(prd_text)
        assert result.group == "my-group"
        assert result.depends_on_groups == ("group-a", "group-b")
        assert result.depends_on_issues == (42, 43)
        assert result.depends_on_prds == ()
        assert result.gate_type == "hard"
        assert result.notes == "wait for upstream"

    def test_parses_markdown_list_fields(self) -> None:
        prd_text = """
## Delivery Dependencies

- Group: my-group
- Depends on groups:
  - group-a
  - group-b
- Depends on tasks/issues:
  - #42
  - 43
  - tasks/pending/P2-FEAT-20260527-190923-prd-from-issue.md
- Gate type: hard
- Notes:
  - wait for upstream
  - publish order matters
"""
        result = parse_delivery_dependencies(prd_text)
        assert result.group == "my-group"
        assert result.depends_on_groups == ("group-a", "group-b")
        assert result.depends_on_issues == (42, 43)
        assert result.depends_on_prds == (
            "tasks/pending/P2-FEAT-20260527-190923-prd-from-issue.md",
        )
        assert result.gate_type == "hard"
        assert result.notes == "wait for upstream publish order matters"

    def test_none_dependency_values_are_treated_as_empty(self) -> None:
        prd_text = """
## Delivery Dependencies

- Group: no-deps
- Depends on groups:
  - none
- Depends on tasks/issues: none
- Gate type: none
"""
        result = parse_delivery_dependencies(prd_text)
        assert result.group == "no-deps"
        assert result.depends_on_groups == ()
        assert result.depends_on_issues == ()
        assert result.depends_on_prds == ()
        assert result.gate_type == "none"

    def test_prd_filename_references_are_preserved(self) -> None:
        prd_text = """
## Delivery Dependencies

- Depends on tasks/issues:
  - P2-FEAT-20260527-190923-prd-from-issue
- Gate type: hard
"""
        result = parse_delivery_dependencies(prd_text)

        assert result.depends_on_issues == ()
        assert result.depends_on_prds == ("P2-FEAT-20260527-190923-prd-from-issue",)

    def test_prd_reference_with_code_span_and_note_is_cleaned(self) -> None:
        prd_text = """
## Delivery Dependencies

- Depends on tasks/issues:
  - `tasks/archive/P1-FEAT-20260611-205725-agent-runner-unified-ops-console.md`（已完成；提供管理终端 shell、仓库 registry、进程/审计模式）
  - `tasks/archive/P1-FEAT-20260614-200054-frontend-prd-roadmap.md` (已完成; 提供 `/roadmap` 页面)
- Gate type: none
"""
        result = parse_delivery_dependencies(prd_text)

        assert result.depends_on_issues == ()
        assert result.depends_on_prds == (
            "tasks/archive/P1-FEAT-20260611-205725-agent-runner-unified-ops-console.md",
            "tasks/archive/P1-FEAT-20260614-200054-frontend-prd-roadmap.md",
        )

    def test_soft_gate(self) -> None:
        prd_text = """
## Delivery Dependencies

- Gate type: soft
"""
        result = parse_delivery_dependencies(prd_text)
        assert result.gate_type == "soft"

    def test_none_gate(self) -> None:
        prd_text = """
## Delivery Dependencies

- Gate type: none
"""
        result = parse_delivery_dependencies(prd_text)
        assert result.gate_type == "none"

    def test_section_ends_at_next_header(self) -> None:
        prd_text = """
## Delivery Dependencies

- Group: g1

## Some Other Section

- Group: g2
"""
        result = parse_delivery_dependencies(prd_text)
        assert result.group == "g1"


# ---------------------------------------------------------------------------
# parse_dependency_marker
# ---------------------------------------------------------------------------


class TestParseDependencyMarker:
    def test_no_marker_returns_none(self) -> None:
        assert parse_dependency_marker("No markers here") is None

    def test_parses_issue_numbers(self) -> None:
        body = "<!-- iar:depends-on #42 #99 -->"
        result = parse_dependency_marker(body)
        assert result == DependencyDeclaration(issue_numbers=(42, 99))

    def test_parses_groups(self) -> None:
        body = "<!-- iar:depends-on group:alpha group:beta -->"
        result = parse_dependency_marker(body)
        assert result == DependencyDeclaration(groups=("alpha", "beta"))

    def test_parses_mixed(self) -> None:
        body = "<!-- iar:depends-on #7 group:gamma #12 -->"
        result = parse_dependency_marker(body)
        assert result == DependencyDeclaration(
            issue_numbers=(7, 12),
            groups=("gamma",),
        )

    def test_multiple_markers(self) -> None:
        body = "<!-- iar:depends-on #1 -->\n<!-- iar:depends-on #2 -->"
        result = parse_dependency_marker(body)
        assert result == DependencyDeclaration(issue_numbers=(1, 2))


# ---------------------------------------------------------------------------
# format_dependency_marker
# ---------------------------------------------------------------------------


class TestFormatDependencyMarker:
    def test_empty_returns_empty(self) -> None:
        assert format_dependency_marker() == ""

    def test_issues_only(self) -> None:
        assert (
            format_dependency_marker(issue_numbers=(42,))
            == "<!-- iar:depends-on #42 -->"
        )

    def test_groups_only(self) -> None:
        assert (
            format_dependency_marker(groups=("g1",))
            == "<!-- iar:depends-on group:g1 -->"
        )

    def test_mixed(self) -> None:
        assert (
            format_dependency_marker(issue_numbers=(1,), groups=("g2",))
            == "<!-- iar:depends-on #1 group:g2 -->"
        )


# ---------------------------------------------------------------------------
# comment deduplication helpers
# ---------------------------------------------------------------------------


class TestCommentDeduplication:
    def test_canonical_blockers_hash_stable(self) -> None:
        b1 = DependencyBlocker("issue", "42", "OPEN")
        b2 = DependencyBlocker("group", "g1", "1 open")
        h1 = _canonical_blockers_hash((b1, b2))
        h2 = _canonical_blockers_hash((b1, b2))
        assert h1 == h2
        assert len(h1) == 16

    def test_parse_latest_dependency_wait_marker(self) -> None:
        comments = [
            "old\n<!-- iar:dependency-wait blockers=abc123 -->",
            "new\n<!-- iar:dependency-wait blockers=def456 -->",
        ]
        assert parse_latest_dependency_wait_marker(comments) == "def456"

    def test_parse_latest_none(self) -> None:
        assert parse_latest_dependency_wait_marker(["no marker"]) is None


# ---------------------------------------------------------------------------
# evaluate_dependencies
# ---------------------------------------------------------------------------


class FakeGitHubClientForDeps:
    """Minimal fake for dependency evaluation tests."""

    def __init__(self, issues: dict[int, IssueSummary] | None = None) -> None:
        self.issues = issues or {}
        self.group_issues: dict[str, list[IssueSummary]] = {}
        self.calls: list[dict] = []

    def get_issue(self, issue_number: int) -> IssueSummary:
        self.calls.append({"method": "get_issue", "issue_number": issue_number})
        if issue_number not in self.issues:
            raise RuntimeError(f"Issue #{issue_number} not found")
        return self.issues[issue_number]

    def list_issues_by_label(
        self, label: str, limit: int, state: str = "all"
    ) -> list[IssueSummary]:
        self.calls.append(
            {"method": "list_issues_by_label", "label": label, "limit": limit}
        )
        return self.group_issues.get(label, [])


class TestEvaluateDependencies:
    def test_no_dependencies_satisfied(self) -> None:
        client = FakeGitHubClientForDeps()
        decl = DependencyDeclaration()
        verdict = evaluate_dependencies(decl, client, LabelConfig())
        assert verdict.satisfied is True
        assert verdict.blockers == ()

    def test_issue_dependency_closed(self) -> None:
        client = FakeGitHubClientForDeps(
            issues={
                42: IssueSummary(
                    number=42,
                    title="Done",
                    url="",
                    body="",
                    labels=(),
                    state="CLOSED",
                )
            }
        )
        decl = DependencyDeclaration(issue_numbers=(42,))
        verdict = evaluate_dependencies(decl, client, LabelConfig())
        assert verdict.satisfied is True

    def test_issue_dependency_open_blocked(self) -> None:
        client = FakeGitHubClientForDeps(
            issues={
                42: IssueSummary(
                    number=42,
                    title="Open",
                    url="",
                    body="",
                    labels=(),
                    state="OPEN",
                )
            }
        )
        decl = DependencyDeclaration(issue_numbers=(42,))
        verdict = evaluate_dependencies(decl, client, LabelConfig())
        assert verdict.satisfied is False
        assert verdict.blockers == (
            DependencyBlocker(blocker_type="issue", target="42", current_state="OPEN"),
        )

    def test_group_dependency_all_closed(self) -> None:
        client = FakeGitHubClientForDeps()
        client.group_issues["task-group/g1"] = [
            IssueSummary(
                number=1, title="A", url="", body="", labels=(), state="CLOSED"
            ),
        ]
        decl = DependencyDeclaration(groups=("g1",))
        verdict = evaluate_dependencies(decl, client, LabelConfig())
        assert verdict.satisfied is True

    def test_group_dependency_open_blocked(self) -> None:
        client = FakeGitHubClientForDeps()
        client.group_issues["task-group/g1"] = [
            IssueSummary(number=1, title="A", url="", body="", labels=(), state="OPEN"),
        ]
        decl = DependencyDeclaration(groups=("g1",))
        verdict = evaluate_dependencies(decl, client, LabelConfig())
        assert verdict.satisfied is False
        assert verdict.blockers[0].blocker_type == "group"

    def test_empty_group_blocked(self) -> None:
        client = FakeGitHubClientForDeps()
        client.group_issues["task-group/g1"] = []
        decl = DependencyDeclaration(groups=("g1",))
        verdict = evaluate_dependencies(decl, client, LabelConfig())
        assert verdict.satisfied is False
        assert verdict.empty_group_names == ("g1",)
        assert verdict.blockers[0].current_state == "empty"

    def test_failed_upstream_detected(self) -> None:
        client = FakeGitHubClientForDeps(
            issues={
                42: IssueSummary(
                    number=42,
                    title="Failed",
                    url="",
                    body="",
                    labels=("agent/failed",),
                    state="OPEN",
                )
            }
        )
        decl = DependencyDeclaration(issue_numbers=(42,))
        verdict = evaluate_dependencies(decl, client, LabelConfig())
        assert verdict.has_failed_or_blocked_upstream is True


# ---------------------------------------------------------------------------
# mark_dependency_waiting / clear_dependency_waiting
# ---------------------------------------------------------------------------


class TestWaitingSideEffects:
    def test_dry_run_only_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        from tests.conftest import FakeGitHubClient

        client = FakeGitHubClient()
        issue = IssueSummary(number=1, title="T", url="", body="", labels=())
        verdict = DependencyVerdict(
            satisfied=False,
            blockers=(DependencyBlocker("issue", "42", "OPEN"),),
        )
        with caplog.at_level("INFO"):
            mark_dependency_waiting(
                issue=issue,
                verdict=verdict,
                github_client=client,
                labels_config=LabelConfig(),
                dry_run=True,
            )
        assert "DRY RUN" in caplog.text
        assert not any(c["method"] == "edit_issue_labels" for c in client.calls)

    def test_adds_waiting_label(self) -> None:
        from tests.conftest import FakeGitHubClient

        client = FakeGitHubClient()
        issue = IssueSummary(number=1, title="T", url="", body="", labels=())
        verdict = DependencyVerdict(
            satisfied=False,
            blockers=(DependencyBlocker("issue", "42", "OPEN"),),
        )
        mark_dependency_waiting(
            issue=issue,
            verdict=verdict,
            github_client=client,
            labels_config=LabelConfig(),
            dry_run=False,
        )
        label_call = [c for c in client.calls if c["method"] == "edit_issue_labels"]
        assert len(label_call) == 1
        assert "agent/waiting" in label_call[0]["add"]

    def test_comment_deduplication(self) -> None:
        from tests.conftest import FakeGitHubClient

        client = FakeGitHubClient()
        client.comment_issue(1, "first\n<!-- iar:dependency-wait blockers=abc -->")
        issue = IssueSummary(number=1, title="T", url="", body="", labels=())
        # Same blockers as stored in comment above
        verdict = DependencyVerdict(
            satisfied=False,
            blockers=(DependencyBlocker("issue", "42", "OPEN"),),
        )
        # Force hash to match by setting up same comment
        client._issue_comments[1] = [
            f"first\n{format_dependency_wait_marker(verdict.blockers)}"
        ]
        mark_dependency_waiting(
            issue=issue,
            verdict=verdict,
            github_client=client,
            labels_config=LabelConfig(),
            dry_run=False,
        )
        # Only label edit, no new comment because blockers unchanged
        comment_calls = [c for c in client.calls if c["method"] == "comment_issue"]
        assert len(comment_calls) == 1  # the setup comment only

    def test_clear_waiting_removes_label(self) -> None:
        from tests.conftest import FakeGitHubClient

        client = FakeGitHubClient()
        issue = IssueSummary(
            number=1,
            title="T",
            url="",
            body="",
            labels=("agent/waiting",),
        )
        clear_dependency_waiting(
            issue=issue,
            github_client=client,
            labels_config=LabelConfig(),
            dry_run=False,
        )
        label_call = [c for c in client.calls if c["method"] == "edit_issue_labels"]
        assert len(label_call) == 1
        assert "agent/waiting" in label_call[0]["remove"]

    def test_clear_waiting_noop_when_no_label(self) -> None:
        from tests.conftest import FakeGitHubClient

        client = FakeGitHubClient()
        issue = IssueSummary(number=1, title="T", url="", body="", labels=())
        clear_dependency_waiting(
            issue=issue,
            github_client=client,
            labels_config=LabelConfig(),
            dry_run=False,
        )
        assert not any(c["method"] == "edit_issue_labels" for c in client.calls)


# ---------------------------------------------------------------------------
# build_waiting_comment
# ---------------------------------------------------------------------------


class TestBuildWaitingComment:
    def test_includes_blockers(self) -> None:
        verdict = DependencyVerdict(
            satisfied=False,
            blockers=(
                DependencyBlocker("issue", "42", "OPEN"),
                DependencyBlocker("group", "g1", "2 open"),
            ),
        )
        comment = build_waiting_comment(verdict, 1, LabelConfig())
        assert "Issue #42" in comment
        assert "Group ``g1``" in comment
        assert "iar:dependency-wait" in comment

    def test_empty_group_warning(self) -> None:
        verdict = DependencyVerdict(
            satisfied=False,
            blockers=(DependencyBlocker("group", "g1", "empty"),),
            empty_group_names=("g1",),
        )
        comment = build_waiting_comment(verdict, 1, LabelConfig())
        assert "empty group" in comment
        assert "possible typo" in comment

    def test_empty_group_includes_resolution_guidance(self) -> None:
        verdict = DependencyVerdict(
            satisfied=False,
            blockers=(DependencyBlocker("group", "g1", "empty"),),
            empty_group_names=("g1",),
        )
        comment = build_waiting_comment(verdict, 1, LabelConfig())
        assert "How to resolve" in comment
        assert "Empty group" in comment
        # Names all three concrete fixes.
        assert "iar:depends-on" in comment
        assert "Depends on groups" in comment

    def test_upstream_failure_warning(self) -> None:
        verdict = DependencyVerdict(
            satisfied=False,
            blockers=(DependencyBlocker("issue", "42", "OPEN"),),
            has_failed_or_blocked_upstream=True,
        )
        comment = build_waiting_comment(verdict, 1, LabelConfig())
        assert "Upstream failure detected" in comment

    def test_upstream_failure_includes_resolution_guidance(self) -> None:
        verdict = DependencyVerdict(
            satisfied=False,
            blockers=(DependencyBlocker("issue", "42", "OPEN"),),
            has_failed_or_blocked_upstream=True,
        )
        comment = build_waiting_comment(verdict, 1, LabelConfig())
        assert "How to resolve" in comment
        assert "Upstream failure" in comment
        assert "agent/failed" in comment
        # Open-issue blocker also produces guidance.
        assert "Open upstream" in comment

    def test_no_resolution_section_when_no_blockers(self) -> None:
        verdict = DependencyVerdict(satisfied=True, blockers=())
        comment = build_waiting_comment(verdict, 1, LabelConfig())
        assert "How to resolve" not in comment


# ---------------------------------------------------------------------------
# _resolve_dependencies (create_issue_from_prd helper)
# ---------------------------------------------------------------------------


class TestResolveDependencies:
    def test_no_dependencies(self) -> None:
        gate, issues, groups = _resolve_dependencies("# PRD\n")
        assert gate == "none"
        assert issues == ()
        assert groups == ()

    def test_prd_only(self) -> None:
        prd = """
## Delivery Dependencies

- Group: g1
- Depends on tasks/issues: #42
- Gate type: hard
"""
        gate, issues, groups = _resolve_dependencies(prd)
        assert gate == "hard"
        assert issues == (42,)

    def test_cli_overrides(self) -> None:
        prd = "# PRD\n"
        gate, issues, groups = _resolve_dependencies(
            prd, depends_on=(99,), depends_on_group=("extra",)
        )
        assert issues == (99,)
        assert groups == ("extra",)

    def test_merge_and_dedup(self) -> None:
        prd = """
## Delivery Dependencies

- Depends on tasks/issues: #1
- Gate type: hard
"""
        gate, issues, groups = _resolve_dependencies(prd, depends_on=(1, 2))
        assert issues == (1, 2)

    def test_explicit_marker_compat(self) -> None:
        prd = "<!-- iar:depends-on #5 group:g3 -->\n# PRD\n"
        gate, issues, groups = _resolve_dependencies(prd)
        assert issues == (5,)
        assert groups == ("g3",)


# ---------------------------------------------------------------------------
# Fail-fast validation
# ---------------------------------------------------------------------------


class TestFailFastValidation:
    def test_invalid_gate_type_raises(self) -> None:
        prd_text = """
## Delivery Dependencies

- Gate type: maybe
"""
        with pytest.raises(ValueError, match="Invalid 'Gate type'"):
            parse_delivery_dependencies(prd_text)

    def test_plain_text_task_reference_is_preserved_for_materialization(self) -> None:
        prd_text = """
## Delivery Dependencies

- Depends on tasks/issues: foo
- Gate type: hard
"""
        result = parse_delivery_dependencies(prd_text)

        assert result.depends_on_issues == ()
        assert result.depends_on_prds == ("foo",)

    def test_case_insensitive_gate_type_normalised(self) -> None:
        prd_text = """
## Delivery Dependencies

- Gate type: HARD
"""
        result = parse_delivery_dependencies(prd_text)
        assert result.gate_type == "hard"

    def test_unknown_field_raises(self) -> None:
        prd_text = """
## Delivery Dependencies

- Grou: my-group
"""
        with pytest.raises(ValueError, match="Unknown field"):
            parse_delivery_dependencies(prd_text)

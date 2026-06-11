"""Read-only Agent Runner monitoring use cases.

本模块是 Agent Runner 监控面板的核心业务逻辑：
- `build_overview`：按仓库汇总队列状态、最新 event 和异常计数。
- `build_issue_detail`：单个 Issue 的 label、PR 上下文、worktree 状态、event timeline、anomaly 和建议 CLI。
- `parse_event_timeline`：从 Issue comments 解析所有 iar:event marker 作为事件时间线。
- 异常检测规则：label_pr_mismatch、pr_dirty_in_review、dirty_worktree_mismatch、event_label_mismatch。

按照四层依赖方向，本模块属于 core/use_cases/，只依赖
``core/shared/interfaces`` 和 ``core/use_cases`` 内部的 helper，不导入
``backend.infrastructure`` 或 ``backend.engines``。这样
``api/`` 路由可以继续作为 thin adapter 委托到本模块。
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING

from backend.core.shared.interfaces.agent_runner import (
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    LabelConfig,
    PullRequestContext,
    ReviewEventMarker,
)
from backend.core.use_cases.agent_runner_events import parse_latest_event_marker

if TYPE_CHECKING:
    from pathlib import Path

_logger = logging.getLogger(__name__)

QUEUE_LABELS: tuple[str, ...] = (
    "agent/ready",
    "agent/running",
    "agent/supervising",
    "agent/review",
    "agent/failed",
    "agent/blocked",
)


@dataclass(frozen=True)
class EventTimelineEntry:
    """A single ordered entry in an Issue's event timeline."""

    phase: str
    cycle: int
    comment_index: int
    action: str | None = None
    head_sha: str | None = None
    pr_branch: str | None = None
    checks_state: str | None = None
    mergeable: bool | None = None
    raw_marker: str = ""


@dataclass(frozen=True)
class WorktreeStatus:
    """Read-only snapshot of a worktree's status."""

    exists: bool
    path: str = ""
    branch: str = ""
    head_sha: str = ""
    is_clean: bool = True
    dirty_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Anomaly:
    """A detected state inconsistency in the monitoring dashboard."""

    type: str
    severity: str
    message: str
    suggested_cli: tuple[str, ...] = ()


@dataclass(frozen=True)
class IssueMonitoringSnapshot:
    """Aggregated monitoring snapshot for a single Issue."""

    number: int
    title: str
    url: str
    labels: tuple[str, ...]
    state: str
    primary_label: str
    pr: dict | None
    worktree: WorktreeStatus
    timeline: tuple[EventTimelineEntry, ...]
    latest_event: ReviewEventMarker | None
    anomalies: tuple[Anomaly, ...]
    suggested_cli_commands: tuple[str, ...]
    has_anomaly: bool
    anomaly_types: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryMonitoringOverview:
    """Aggregated monitoring overview for a single repository."""

    repo_id: str
    display_name: str
    enabled: bool
    base_branch: str
    remote: str
    health: dict
    queue_counts: dict[str, int]
    labels: dict[str, str]
    issues: tuple[IssueMonitoringSnapshot, ...]
    anomaly_count: int
    anomaly_summary: dict[str, int]
    scanned_at: str = ""


@dataclass(frozen=True)
class MonitoringResult:
    """Top-level result returned to the API layer."""

    repositories: tuple[RepositoryMonitoringOverview, ...]
    scanned_at: str = ""


@dataclass(frozen=True)
class AnomalyDetectionContext:
    """Inputs to the anomaly detection rules."""

    issue: IssueSummary
    labels: tuple[str, ...]
    primary_label: str
    pr_context: PullRequestContext | None
    worktree: WorktreeStatus
    latest_event: ReviewEventMarker | None
    config: AppConfig


# ─────────────────────────────────────────────────────────────────────────────
# Event timeline parsing
# ─────────────────────────────────────────────────────────────────────────────

_EVENT_MARKER_LINE_PATTERN = re.compile(
    r"<!--\s*iar:event\s+([^>]+?)\s*-->",
    re.DOTALL,
)


def _coerce_int(raw_value: str | None) -> int | None:
    """Parse a string into int, returning None on failure."""
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(raw_value: str | None) -> bool | None:
    """Parse a true/false marker attribute, returning None on absence."""
    if raw_value is None:
        return None
    normalized = raw_value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def _parse_marker_attributes(
    attributes_text: str,
) -> dict[str, str]:
    """Parse ``key=value`` pairs separated by whitespace from a marker body."""
    attributes: dict[str, str] = {}
    for attribute_match in re.finditer(r"([\w]+)=([^\s>]+)", attributes_text):
        attributes[attribute_match.group(1)] = attribute_match.group(2)
    return attributes


def parse_event_timeline(comments: Iterable[str]) -> list[EventTimelineEntry]:
    """Parse all iar:event markers from Issue comments, oldest first.

    Args:
        comments: Iterable of comment bodies in chronological order. Bodies
            that do not contain an ``iar:event`` marker are skipped.

    Returns:
        A list of :class:`EventTimelineEntry` ordered from oldest to newest.
        Comments may contain multiple markers; each is treated as its own
        timeline entry.
    """
    timeline: list[EventTimelineEntry] = []
    for comment_index, comment_body in enumerate(comments):
        if not comment_body:
            continue
        for marker_match in _EVENT_MARKER_LINE_PATTERN.finditer(comment_body):
            attributes = _parse_marker_attributes(marker_match.group(1))
            phase = attributes.get("phase")
            cycle = _coerce_int(attributes.get("cycle"))
            if phase is None or cycle is None:
                continue
            timeline.append(
                EventTimelineEntry(
                    phase=phase,
                    cycle=cycle,
                    comment_index=comment_index,
                    action=attributes.get("action"),
                    head_sha=attributes.get("head"),
                    pr_branch=attributes.get("pr_branch"),
                    checks_state=attributes.get("checks_state"),
                    mergeable=_coerce_bool(attributes.get("mergeable")),
                    raw_marker=marker_match.group(0),
                )
            )
    return timeline


# ─────────────────────────────────────────────────────────────────────────────
# Worktree inspection
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_worktree_path_command(
    worktree_path_command: str,
    issue_number: int,
) -> list[str]:
    """Render and tokenize a worktree path_command template for an issue number."""
    rendered = worktree_path_command.replace("{issue_number}", str(issue_number))
    try:
        return shlex.split(rendered)
    except ValueError:
        return [rendered]


def _parse_shell_quoted_path(command_line: str) -> str:
    """Extract the last single-quoted path from a shell-style command line.

    The default ``path_command`` uses ``bash -c 'echo \"...\"'``; the actual
    path is double-quoted inside a single-quoted shell string. We only need
    a coarse extraction good enough for monitoring — if parsing fails, the
    caller treats it as "worktree not present".
    """
    quoted_double = re.findall(r'"([^"]+)"', command_line)
    if quoted_double:
        return quoted_double[-1]
    quoted_single = re.findall(r"'([^']+)'", command_line)
    if quoted_single:
        return quoted_single[-1]
    return command_line.strip().splitlines()[-1].strip()


def _read_worktree_status(
    *,
    worktree_path_command: str,
    issue_number: int,
    expected_branch: str | None,
    repo_path: Path,
    process_runner: IProcessRunner,
) -> WorktreeStatus:
    """Read a worktree's branch, HEAD SHA, and clean status for monitoring.

    Failures in any individual step degrade to "unknown" rather than raising,
    so a missing worktree does not break the whole monitoring view.
    """
    try:
        path_command = _resolve_worktree_path_command(
            worktree_path_command, issue_number
        )
        path_result = process_runner.run(
            path_command,
            cwd=repo_path,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - monitoring is best-effort.
        _logger.info("Worktree path lookup failed for Issue #%d: %s", issue_number, exc)
        return WorktreeStatus(exists=False)

    if path_result.return_code != 0 or not path_result.stdout.strip():
        return WorktreeStatus(exists=False)

    from pathlib import Path as _Path

    worktree_path = _Path(_parse_shell_quoted_path(path_result.stdout))
    if not worktree_path.exists():
        return WorktreeStatus(exists=False, path=str(worktree_path))

    base_status = WorktreeStatus(exists=True, path=str(worktree_path))
    try:
        branch_result = process_runner.run(
            ["git", "branch", "--show-current"],
            cwd=worktree_path,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.info("Worktree branch lookup failed: %s", exc)
        return base_status
    if branch_result.return_code != 0:
        return base_status
    branch = branch_result.stdout.strip()
    if expected_branch and branch and branch != expected_branch:
        return WorktreeStatus(
            exists=True,
            path=str(worktree_path),
            branch=branch,
        )

    try:
        head_result = process_runner.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_path,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.info("Worktree HEAD lookup failed: %s", exc)
        return WorktreeStatus(exists=True, path=str(worktree_path), branch=branch)
    head_sha = head_result.stdout.strip() if head_result.return_code == 0 else ""

    try:
        status_result = process_runner.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.info("Worktree status lookup failed: %s", exc)
        return WorktreeStatus(
            exists=True,
            path=str(worktree_path),
            branch=branch,
            head_sha=head_sha,
        )

    porcelain_lines = [
        line for line in (status_result.stdout or "").splitlines() if line
    ]
    dirty_files: tuple[str, ...] = tuple(
        _parse_status_path(line) for line in porcelain_lines
    )
    return WorktreeStatus(
        exists=True,
        path=str(worktree_path),
        branch=branch,
        head_sha=head_sha,
        is_clean=not porcelain_lines,
        dirty_files=dirty_files,
    )


def _parse_status_path(porcelain_line: str) -> str:
    """Extract the changed path from a ``git status --porcelain`` line."""
    raw_path = porcelain_line[3:] if len(porcelain_line) > 3 else porcelain_line
    if " -> " in raw_path:
        return raw_path.split(" -> ", maxsplit=1)[1]
    return raw_path.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Suggested CLI derivation
# ─────────────────────────────────────────────────────────────────────────────


def _derive_suggested_cli(
    *,
    primary_label: str,
    has_pr: bool,
    pr_dirty: bool,
    worktree_dirty: bool,
    event_label_mismatch: bool,
) -> tuple[str, ...]:
    """Build a deduplicated list of recommended CLI recovery commands."""
    suggested: list[str] = []
    seen: set[str] = set()

    def _add(command: str) -> None:
        if command not in seen:
            suggested.append(command)
            seen.add(command)

    if has_pr and primary_label in {"agent/ready", "agent/running"}:
        _add("iar labels sync")
    if event_label_mismatch:
        _add("iar labels sync")
    if has_pr and primary_label not in {
        "agent/supervising",
        "agent/review",
        "agent/blocked",
        "agent/failed",
    }:
        _add("iar review --dry-run")
    if pr_dirty:
        _add("iar review")
        _add("iar run --max-issues 1")
    if worktree_dirty and primary_label != "agent/running":
        _add("iar run --dry-run")
    if primary_label == "agent/failed":
        _add("iar run --dry-run")
    if primary_label == "agent/blocked":
        _add("iar review")
    if primary_label == "agent/ready":
        _add("iar run --dry-run")
    return tuple(suggested)


# ─────────────────────────────────────────────────────────────────────────────
# Anomaly detection
# ─────────────────────────────────────────────────────────────────────────────


_POST_PR_LABELS: frozenset[str] = frozenset(
    {
        "agent/supervising",
        "agent/review",
        "agent/blocked",
        "agent/failed",
    }
)


def _phase_implies_label(phase: str) -> str | None:
    """Map an iar:event phase to the label it implies.

    Returns ``None`` when the phase does not pin a label decision.
    """
    if phase == "claimed":
        return "agent/running"
    if phase == "implementation_complete":
        return "agent/running"
    if phase == "pre_push_review":
        return "agent/running"
    if phase == "draft_pr_created":
        return "agent/supervising"
    if phase == "post_pr_supervisor":
        return "agent/supervising"
    if phase == "post_pr_rework_requested":
        return "agent/running"
    if phase == "rebase_repair_complete":
        return "agent/supervising"
    return None


def detect_anomalies(context: AnomalyDetectionContext) -> tuple[Anomaly, ...]:
    """Return the list of monitoring anomalies for a single Issue.

    Args:
        context: Aggregated inputs (Issue, PR, worktree, latest event).

    Returns:
        A tuple of :class:`Anomaly` entries. Order is stable for tests.
    """
    anomalies: list[Anomaly] = []

    has_pr = context.pr_context is not None
    pr_dirty = bool(
        context.pr_context is not None and context.pr_context.mergeable is False
    )
    worktree_dirty = bool(context.worktree.exists and not context.worktree.is_clean)

    if has_pr and context.primary_label not in _POST_PR_LABELS:
        anomalies.append(
            Anomaly(
                type="label_pr_mismatch",
                severity="warning",
                message=("PR exists but Issue label does not reflect post-PR state."),
                suggested_cli=("iar labels sync", "iar review --dry-run"),
            )
        )

    if context.primary_label == "agent/review" and pr_dirty:
        anomalies.append(
            Anomaly(
                type="pr_dirty_in_review",
                severity="error",
                message=("PR is dirty/conflicted while Issue is in review state."),
                suggested_cli=("iar review", "iar run --max-issues 1"),
            )
        )

    if worktree_dirty and context.primary_label != "agent/running":
        anomalies.append(
            Anomaly(
                type="dirty_worktree_mismatch",
                severity="warning",
                message=(
                    "Worktree has uncommitted changes but Issue is not in "
                    "running state."
                ),
                suggested_cli=("iar run --dry-run", "git status"),
            )
        )

    if context.latest_event is not None and context.latest_event.phase:
        implied = _phase_implies_label(context.latest_event.phase)
        if (
            implied is not None
            and implied != context.primary_label
            and context.primary_label in _POST_PR_LABELS
        ):
            anomalies.append(
                Anomaly(
                    type="event_label_mismatch",
                    severity="warning",
                    message=(
                        "Latest event marker suggests a different state than "
                        "current label."
                    ),
                    suggested_cli=("iar labels sync",),
                )
            )

    return tuple(anomalies)


# ─────────────────────────────────────────────────────────────────────────────
# PR context → monitoring dict
# ─────────────────────────────────────────────────────────────────────────────


def _pr_context_to_dict(pr_context: PullRequestContext | None) -> dict | None:
    """Convert a PR context dataclass into a JSON-serializable dict."""
    if pr_context is None:
        return None
    return {
        "number": _extract_pr_number(pr_context.pr_url),
        "url": pr_context.pr_url,
        "branch": pr_context.branch,
        "head_sha": pr_context.head_sha,
        "base_sha": pr_context.base_sha,
        "mergeable": pr_context.mergeable,
        "checks_state": pr_context.checks_state,
        "checks_summary": list(pr_context.checks_summary),
    }


def _extract_pr_number(pr_url: str) -> int | None:
    """Pull a PR number out of a ``https://.../pull/<n>`` URL."""
    match = re.search(r"/pull/(\d+)", pr_url or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Single-issue snapshot
# ─────────────────────────────────────────────────────────────────────────────


def build_issue_snapshot(
    *,
    issue: IssueSummary,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    repo_path: Path,
    comments: list[str] | None = None,
) -> IssueMonitoringSnapshot:
    """Build the monitoring snapshot for a single Issue.

    Reads only — never writes — to GitHub or the local worktree.
    """
    if comments is None:
        comments = github_client.list_issue_comments(issue.number)
    timeline = tuple(parse_event_timeline(comments))
    latest_event = parse_latest_event_marker(comments)
    pr_context = _lookup_pr_context(issue, github_client, comments)
    worktree = _read_worktree_status(
        worktree_path_command=config.worktree.path_command,
        issue_number=issue.number,
        expected_branch=pr_context.branch if pr_context is not None else None,
        repo_path=repo_path,
        process_runner=process_runner,
    )
    primary_label = _resolve_primary_label(issue.labels, config.labels)

    anomaly_context = AnomalyDetectionContext(
        issue=issue,
        labels=issue.labels,
        primary_label=primary_label,
        pr_context=pr_context,
        worktree=worktree,
        latest_event=latest_event,
        config=config,
    )
    anomalies = detect_anomalies(anomaly_context)
    has_anomaly = bool(anomalies)

    return IssueMonitoringSnapshot(
        number=issue.number,
        title=issue.title,
        url=issue.url,
        labels=issue.labels,
        state="open",
        primary_label=primary_label,
        pr=_pr_context_to_dict(pr_context),
        worktree=worktree,
        timeline=timeline,
        latest_event=latest_event,
        anomalies=anomalies,
        suggested_cli_commands=_derive_suggested_cli(
            primary_label=primary_label,
            has_pr=pr_context is not None,
            pr_dirty=bool(pr_context is not None and pr_context.mergeable is False),
            worktree_dirty=bool(worktree.exists and not worktree.is_clean),
            event_label_mismatch=any(
                anomaly.type == "event_label_mismatch" for anomaly in anomalies
            ),
        ),
        has_anomaly=has_anomaly,
        anomaly_types=tuple(anomaly.type for anomaly in anomalies),
    )


def _lookup_pr_context(
    issue: IssueSummary,
    github_client: IGitHubClient,
    comments: list[str] | None = None,
) -> PullRequestContext | None:
    """Find an open PR context for the issue, if any."""
    pr_branch = _extract_pr_branch_from_issue(issue, github_client, comments)
    if pr_branch is None:
        return None
    pr_context = github_client.get_pull_request_context(pr_branch)
    if pr_context is not None:
        return pr_context
    if github_client.find_open_pr_by_head(pr_branch):
        # Fall back to a synthetic minimal context so anomalies can still
        # report a PR exists even when full context lookup fails.
        return PullRequestContext(
            pr_url="",
            branch=pr_branch,
            head_sha="",
            base_sha="",
            mergeable=None,
        )
    return None


def _extract_pr_branch_from_issue(
    issue: IssueSummary,
    github_client: IGitHubClient,
    comments: list[str] | None = None,
) -> str | None:
    """Resolve the PR branch associated with the issue, if any."""
    if comments is None:
        comments = github_client.list_issue_comments(issue.number)
    for comment_body in reversed(comments):
        marker = parse_latest_event_marker([comment_body])
        if marker is not None and marker.pr_branch:
            return marker.pr_branch
        for branch_pattern in (
            r"PR Branch:\s*`([^`]+)`",
            r"Branch:\s*`([^`]+)`",
        ):
            branch_match = re.search(branch_pattern, comment_body)
            if branch_match:
                return branch_match.group(1)
    return None


def _resolve_primary_label(
    labels: tuple[str, ...],
    config_labels: LabelConfig,
) -> str:
    """Return the single primary agent/* label, preferring order over config."""
    priority_order = (
        config_labels.running,
        config_labels.supervising,
        config_labels.review,
        config_labels.failed,
        config_labels.blocked,
        config_labels.ready,
    )
    for label in priority_order:
        if label in labels:
            return label
    for label in labels:
        if label.startswith("agent/"):
            return label
    return labels[0] if labels else ""


# ─────────────────────────────────────────────────────────────────────────────
# Repository overview
# ─────────────────────────────────────────────────────────────────────────────


def _collect_queue_issues(
    *,
    config: AppConfig,
    github_client: IGitHubClient,
    limit_per_label: int = 30,
) -> list[IssueSummary]:
    """Gather all candidate issues across queue labels.

    Uses OR semantics (each label queried separately) so an Issue with any
    of the configured queue labels is considered.
    """
    seen_numbers: set[int] = set()
    collected: list[IssueSummary] = []
    queue_labels = (
        config.labels.ready,
        config.labels.running,
        config.labels.supervising,
        config.labels.review,
        config.labels.failed,
        config.labels.blocked,
    )
    for label in queue_labels:
        try:
            issues = github_client.list_ready_issues(label, limit_per_label)
        except Exception as exc:  # noqa: BLE001 - keep monitoring resilient.
            _logger.info("Queue scan failed for label %s: %s", label, exc)
            continue
        for issue in issues:
            if issue.number in seen_numbers:
                continue
            seen_numbers.add(issue.number)
            collected.append(issue)
    return collected


def _check_gh_available(
    process_runner: IProcessRunner,
    cwd: Path,
) -> bool:
    """Return whether the GitHub CLI is available on PATH."""
    try:
        result = process_runner.run(["gh", "--version"], cwd=cwd, check=False)
    except Exception:  # noqa: BLE001
        return False
    return result.return_code == 0


def _check_repo_path_exists(repo_path: Path) -> bool:
    """Return whether the configured repo path exists on disk."""
    return repo_path.exists() and repo_path.is_dir()


def _check_publish_remote_exists(
    repo_path: Path,
    remote: str,
    process_runner: IProcessRunner,
) -> bool:
    """Return whether the configured publish remote is configured."""
    try:
        result = process_runner.run(
            ["git", "remote", "get-url", remote],
            cwd=repo_path,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return False
    return result.return_code == 0 and bool(result.stdout.strip())


def _compute_queue_counts(
    issues: Iterable[IssueSummary],
    config: AppConfig,
) -> dict[str, int]:
    """Tally issues by their primary queue label."""
    counts: dict[str, int] = {
        config.labels.ready: 0,
        config.labels.running: 0,
        config.labels.supervising: 0,
        config.labels.review: 0,
        config.labels.failed: 0,
        config.labels.blocked: 0,
    }
    for issue in issues:
        primary = _resolve_primary_label(issue.labels, config.labels)
        if primary in counts:
            counts[primary] += 1
    return counts


def _now_iso() -> str:
    """Return a coarse ISO-8601 timestamp for ``scanned_at`` markers."""
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def build_repository_overview(
    *,
    repo_id: str,
    display_name: str,
    enabled: bool,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    repo_path: Path,
) -> RepositoryMonitoringOverview:
    """Build a per-repository monitoring overview."""
    queue_issues = _collect_queue_issues(config=config, github_client=github_client)
    issue_snapshots: list[IssueMonitoringSnapshot] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_issue = {
            executor.submit(
                build_issue_snapshot,
                issue=issue,
                config=config,
                github_client=github_client,
                process_runner=process_runner,
                repo_path=repo_path,
            ): issue
            for issue in queue_issues
        }
        for future in as_completed(future_to_issue):
            issue = future_to_issue[future]
            try:
                snapshot = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad Issue must not blank the page.
                _logger.warning(
                    "Failed to build snapshot for Issue #%d: %s", issue.number, exc
                )
                continue
            issue_snapshots.append(snapshot)

    queue_counts = _compute_queue_counts(queue_issues, config)
    anomaly_count = sum(1 for snap in issue_snapshots if snap.has_anomaly)
    anomaly_summary: dict[str, int] = {"warning": 0, "error": 0}
    for snap in issue_snapshots:
        for anomaly in snap.anomalies:
            anomaly_summary[anomaly.severity] = (
                anomaly_summary.get(anomaly.severity, 0) + 1
            )

    health = {
        "gh_available": _check_gh_available(process_runner, repo_path),
        "repo_path_exists": _check_repo_path_exists(repo_path),
        "publish_remote_exists": _check_publish_remote_exists(
            repo_path, config.git.remote, process_runner
        ),
    }

    return RepositoryMonitoringOverview(
        repo_id=repo_id,
        display_name=display_name,
        enabled=enabled,
        base_branch=config.git.base_branch,
        remote=config.git.remote,
        health=health,
        queue_counts=queue_counts,
        labels={
            "ready": config.labels.ready,
            "running": config.labels.running,
            "supervising": config.labels.supervising,
            "review": config.labels.review,
            "failed": config.labels.failed,
            "blocked": config.labels.blocked,
        },
        issues=tuple(issue_snapshots),
        anomaly_count=anomaly_count,
        anomaly_summary=anomaly_summary,
        scanned_at=_now_iso(),
    )


def build_overview(
    *,
    repositories: Iterable,
    github_client_factory,
    process_runner: IProcessRunner,
) -> MonitoringResult:
    """Build the cross-repository monitoring overview.

    Args:
        repositories: Iterable of :class:`RepositoryRunContext` from
            :func:`backend.engines.agent_runner.factory.resolve_repository_targets`.
        github_client_factory: Callable accepting ``repo_path`` and returning
            a :class:`IGitHubClient` implementation.
        process_runner: Shared process runner.

    Returns:
        A :class:`MonitoringResult` containing per-repository overviews.
    """
    repository_overviews: list[RepositoryMonitoringOverview] = []
    for repository_context in repositories:
        github_client = github_client_factory(repository_context.repo_path)
        try:
            overview = build_repository_overview(
                repo_id=repository_context.repo_id,
                display_name=repository_context.display_name,
                enabled=True,
                config=repository_context.config,
                github_client=github_client,
                process_runner=process_runner,
                repo_path=repository_context.repo_path,
            )
        except Exception as exc:  # noqa: BLE001 - one bad repository must not blank the page.
            _logger.warning(
                "Failed to build overview for %s: %s",
                repository_context.repo_id,
                exc,
            )
            continue
        repository_overviews.append(overview)

    return MonitoringResult(
        repositories=tuple(repository_overviews),
        scanned_at=_now_iso(),
    )


__all__ = [
    "Anomaly",
    "AnomalyDetectionContext",
    "EventTimelineEntry",
    "IssueMonitoringSnapshot",
    "MonitoringResult",
    "QUEUE_LABELS",
    "RepositoryMonitoringOverview",
    "WorktreeStatus",
    "build_issue_snapshot",
    "build_overview",
    "build_repository_overview",
    "detect_anomalies",
    "parse_event_timeline",
]

"""Agent Runner domain models and value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from backend.core.shared.models.agent_decision import (
    InteractiveDecisionConfig,
    ReplConfig,
)
from backend.core.shared.models.agent_deliberation import DeliberationConfig


@dataclass(frozen=True)
class AgentCommitResult:
    """Result of a successful agent execution with attempt history."""

    verification_results: list[CommandResult]
    attempt_results: list[AttemptResult]


@dataclass(frozen=True)
class CommandResult:
    """Captured subprocess result."""

    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str


class FailureType(Enum):
    """Categorized failure reason from an agent execution attempt."""

    SUCCESS = "success"
    UNCOMMITTED_CHANGES = "uncommitted_changes"
    NO_COMMITS = "no_commits"
    VERIFICATION_FAILED = "verification_failed"
    AGENT_ERROR = "agent_error"
    TRANSIENT = "transient"
    PROVIDER_CAPACITY = "provider_capacity"
    UNRECOVERABLE = "unrecoverable"
    FORBIDDEN_BLOCKED = "forbidden_blocked"


@dataclass(frozen=True)
class AttemptResult:
    """Record of a single agent execution attempt.

    Attributes:
        attempt_number: 1-based attempt index within a single agent's run.
        failure_type: Classified outcome of the attempt.
        recovered: Whether this attempt recovered from a prior failure.
        detail: Human-readable detail rendered into the failure comment.
        agent: Name of the agent that produced this attempt.
        started_at: ISO-8601 UTC timestamp when the attempt started.
        finished_at: ISO-8601 UTC timestamp when the attempt finished.
        duration_seconds: Wall-clock seconds spent in the attempt.
    """

    attempt_number: int
    failure_type: FailureType
    recovered: bool
    detail: str
    agent: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class IssueSummary:
    """GitHub Issue selected for runner execution."""

    number: int
    title: str
    url: str
    body: str
    labels: tuple[str, ...]
    state: str = "OPEN"


@dataclass(frozen=True)
class PullRequestSummary:
    """View-model summary of a Pull Request linked to an Issue.

    Attributes:
        number: PR number within the repository.
        state: Normalized state, one of ``"open"``, ``"draft"``,
            ``"merged"``, ``"closed"``.
        url: Web URL of the PR.
        is_draft: Whether the PR is currently a draft.
        merged: Whether the PR has been merged.
        title: PR title (used by ``--output json`` consumers).
    """

    number: int
    state: str
    url: str
    is_draft: bool
    merged: bool
    title: str


@dataclass(frozen=True)
class IssueWithPulls:
    """View-model row for ``iar issue list`` output.

    Attributes:
        repo: ``owner/name`` identifier when the list spans multiple
            repositories; ``None`` for single-repository listings.
        number: Issue number within the repository.
        title: Issue title.
        state: GitHub state (``"OPEN"`` / ``"CLOSED"``).
        labels: Label names attached to the Issue.
        updated_at: ISO-8601 timestamp of the last update (or empty
            string when the backend cannot supply one).
        url: Web URL of the Issue.
        pulls: Linked Pull Requests, already normalized.
    """

    repo: str | None
    number: int
    title: str
    state: str
    labels: tuple[str, ...]
    updated_at: str
    url: str
    pulls: tuple[PullRequestSummary, ...]


@dataclass(frozen=True)
class LabelConfig:
    """GitHub labels used as runner queue state."""

    ready: str = "agent/ready"
    running: str = "agent/running"
    supervising: str = "agent/supervising"
    review: str = "agent/review"
    failed: str = "agent/failed"
    blocked: str = "agent/blocked"
    waiting: str = "agent/waiting"
    validation_pending: str = "validation/pending"
    validation_passed: str = "validation/passed"
    group_prefix: str = "task-group/"
    rework_prd: str = "agent/rework-prd"
    deliberate: str = "agent/deliberate"
    agent_labels: dict[str, str] = field(
        default_factory=lambda: {
            "codex": "agent/codex",
            "claude": "agent/claude",
            "kimi": "agent/kimi",
        }
    )


@dataclass(frozen=True)
class GitConfig:
    """Git publishing configuration."""

    remote: str = "origin"
    base_branch: str = "main"


@dataclass(frozen=True)
class WorktreeConfig:
    """Commands used to create and locate target worktrees.

    ``base_branch`` is the repository base branch (e.g. ``main``). It is
    passed to the ``create_command`` template so ``git worktree add`` can
    fork from the correct ref. See
    :class:`backend.infrastructure.config.settings.AgentRunnerWorktreeSettings`
    for the matching Pydantic defaults.
    """

    create_command: str = (
        "iar worktree create --branch issue-{issue_number} "
        "--base-branch {base_branch}"
    )
    reuse_command: str = "iar worktree path --branch issue-{issue_number}"
    path_command: str = "iar worktree path --branch issue-{issue_number}"
    base_branch: str = "main"


@dataclass(frozen=True)
class RunnerConfig:
    """Local runner behavior.

    Attributes:
        agent_fallback_order: Ordered agents to try for one Issue after the
            primary agent. Empty disables cross-agent fallback (single-agent
            behavior). The primary agent is prepended and de-duplicated by
            ``resolve_agent_fallback_order``.
        max_agent_switches: Maximum number of agent switches before the Issue
            is marked failed.
        transient_retry_attempts: In-place retries granted to transient
            network/transport errors (Level 1 of the escalation ladder).
        transient_retry_delay_seconds: Backoff between transient retries.
        max_concurrent_issues: Maximum Issues processed in parallel within a
            single daemon pass. ``1`` keeps the sequential path (zero
            regression); ``> 1`` enables the thread-pool parallel path.
        timeout_seconds: Wall-clock timeout for a single agent execution.
        inactivity_timeout_seconds: Kill the agent if it produces no stdout or
            stderr for this many seconds.
        fix_timeout_seconds: Optional shorter timeout for the Fix Agent phase.
            When ``None``, falls back to ``timeout_seconds``.
        recovery_timeout_seconds: Optional timeout for the full Recovery Agent
            phase. When ``None``, falls back to ``timeout_seconds``.
    """

    max_issues: int = 1
    max_concurrent_issues: int = 1
    default_agent: str = "auto"
    max_recovery_attempts: int = 5
    recovery_retry_delay_seconds: int = 30
    agent_fallback_order: tuple[str, ...] = ("claude", "kimi", "codex")
    max_agent_switches: int = 2
    transient_retry_attempts: int = 2
    transient_retry_delay_seconds: int = 10
    timeout_seconds: int = 14400
    fix_timeout_seconds: int | None = None
    recovery_timeout_seconds: int | None = None
    inactivity_timeout_seconds: int = 1200
    verification_commands: tuple[str, ...] = (
        "git diff --check",
        "uv run mkdocs build",
    )


@dataclass(frozen=True)
class SafetyConfig:
    """Safety boundaries enforced before publishing."""

    auto_merge: bool = False
    forbidden_path_patterns: tuple[str, ...] = (
        ".env",
        ".env.*",
        "secrets/*",
        "docker-compose.prod.yml",
    )


@dataclass(frozen=True)
class PromptConfig:
    """Agent prompt template configuration."""

    default_phase: str = "execution"
    phases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationConfig:
    """Realistic Validation evidence gate configuration.

    ``evidence_dir`` is relative to the worktree root and is excluded from
    git tracking via ``info/exclude``, so evidence files can never reach the
    code diff. ``branch_prefix`` names the orphan branches that carry
    evidence to reviewers (``<branch_prefix>issue-<N>``); these branches are
    never merged and are deleted once the Issue closes.

    ``evidence_format_check`` toggles the per-item evidence matching rules
    (every item needs its own ``rv-<n>-*`` file in the format the item
    names); switching it off keeps only the non-empty evidence requirement.
    Individual PRDs can opt out via an ``Evidence Format Waiver: <reason>``
    line in their Realistic Validation section.

    ``language`` controls the fixed labels in prompts and PR evidence comments.
    ``structured_evidence`` enables the ``evidence.json`` manifest requirement
    for new Issues that carry the ``iar:structured-evidence`` marker.
    ``require_negative_control`` (default on) makes the gate reject any
    structured-evidence item lacking a ``negative_control`` (red→green proof);
    set it off to opt out per repository.
    """

    enabled: bool = True
    evidence_dir: str = ".iar/evidence"
    branch_prefix: str = "iar-evidence/"
    evidence_format_check: bool = True
    parse_evidence_format_with_agent: bool = True
    language: str = "zh-CN"
    structured_evidence: bool = True
    require_negative_control: bool = True
    reexecute_commands: bool = True
    reexecute_timeout_seconds: int = 300
    verifier_enabled: bool = False
    verifier_agent: str = "auto"
    verifier_timeout_seconds: int = 1800


@dataclass(frozen=True)
class ReviewFinding:
    """Structured finding emitted by the pre-push reviewer."""

    category: str = ""
    severity: str = ""
    title: str = ""
    description: str = ""
    file: str = ""
    line: int = 0
    recommendation: str = ""


@dataclass(frozen=True)
class PrePrReviewConfig:
    """Pre-PR AI review gate configuration.

    The review runs **after** the implementation commit has been pushed to the
    remote but **before** the Draft PR is created, so reviewer patches are
    pushed to the feature branch while the PR creation gate waits for the
    review to converge.
    """

    enabled: bool = True
    review_agent: str = "auto"
    allow_same_agent: bool = True
    max_attempts: int = 2
    timeout_seconds: int = 1800
    # When the reviewer reports findings but fails to write a commit request,
    # the runner appends a reminder and re-invokes the reviewer up to this
    # many times within the same review cycle. This prevents the runner from
    # giving up just because the reviewer listed problems without producing a
    # patch.
    commit_request_reminder_attempts: int = 1
    # Overrides for the review rules appended after the review packet.
    # Empty tuple means "use the embedded default template" which instructs
    # the reviewer to call the ``code-reviewer`` skill and emit a findings
    # array. Repositories can override individual lines via ``.iar.toml``
    # without forking ``agent_review.py``.
    review_prompt_template: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostPrSupervisorConfig:
    """Post-PR supervisor cycle configuration."""

    enabled: bool = True
    supervisor_agent: str = "auto"
    max_repair_attempts: int = 2
    max_agent_crash_retries: int = 5
    crash_retry_initial_backoff_seconds: int = 30
    crash_retry_max_backoff_seconds: int = 600


@dataclass(frozen=True)
class PullRequestContext:
    """Minimal PR context for supervisor decisions."""

    pr_url: str
    branch: str
    head_sha: str
    base_sha: str
    mergeable: bool | None = None
    checks_state: str | None = None
    checks_summary: tuple[str, ...] = ()
    number: int | None = None
    body: str = ""


@dataclass(frozen=True)
class ReviewEventMarker:
    """Parsed iar:event hidden marker from an Issue comment."""

    version: int
    phase: str
    cycle: int
    head_sha: str | None = None
    base_sha: str | None = None
    pr_branch: str | None = None
    action: str | None = None
    checks_state: str | None = None
    mergeable: bool | None = None
    issue_comments_count: int | None = None
    pr_comments_count: int | None = None
    blocked_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class SupervisorActionResult:
    """Outcome of a single supervisor cycle."""

    action: str
    summary: str = ""
    findings_counts: dict[str, int] = field(default_factory=dict)
    verification_status: str = ""
    head_sha: str | None = None


@dataclass(frozen=True)
class GeneratedContentTargetConfig:
    """Configuration for a single generated-content target."""

    enabled: bool = True
    mode: str = "template"
    output: str = "json"
    title_template: str = ""
    body_template: str = ""
    agent: str = "auto"
    timeout_seconds: int = 120
    prompt: str = ""
    include_commit_log: bool = True
    include_diff_stat: bool = True


@dataclass(frozen=True)
class GeneratedContentConfig:
    """Generated-content configuration for Issues and PRs."""

    enabled: bool = True
    fallback: str = "template"
    max_input_chars: int = 20000
    default_agent: str = "auto"
    issue_from_prd: GeneratedContentTargetConfig = field(
        default_factory=GeneratedContentTargetConfig
    )
    draft_pr: GeneratedContentTargetConfig = field(
        default_factory=GeneratedContentTargetConfig
    )
    prd_from_issue: GeneratedContentTargetConfig = field(
        # PRD 生成没有可用的内置 template，唯一有意义的模式是 agent；
        # agent 不可用时 generate_prd_content 会优雅退回 fallback。
        default_factory=lambda: GeneratedContentTargetConfig(mode="agent")
    )


@dataclass(frozen=True)
class GeneratedIssueContent:
    """Result of generated Issue content."""

    title: str
    body: str
    source: str = "fallback"


@dataclass(frozen=True)
class GeneratedPrContent:
    """Result of generated PR content."""

    title: str
    body: str
    source: str = "fallback"


@dataclass(frozen=True)
class AppConfig:
    """Application configuration."""

    labels: LabelConfig = LabelConfig()
    git: GitConfig = GitConfig()
    worktree: WorktreeConfig = WorktreeConfig()
    runner: RunnerConfig = RunnerConfig()
    safety: SafetyConfig = SafetyConfig()
    validation: ValidationConfig = ValidationConfig()
    prompts: PromptConfig = field(default_factory=PromptConfig)
    pre_pr_review: PrePrReviewConfig = PrePrReviewConfig()
    post_pr_supervisor: PostPrSupervisorConfig = PostPrSupervisorConfig()
    generated_content: GeneratedContentConfig = field(
        default_factory=GeneratedContentConfig
    )
    interactive_decision: InteractiveDecisionConfig = field(
        default_factory=InteractiveDecisionConfig
    )
    repl: ReplConfig = field(default_factory=ReplConfig)
    deliberation: DeliberationConfig = field(default_factory=DeliberationConfig)


@dataclass(frozen=True)
class RepositoryRunContext:
    """Resolved target repository with merged configuration."""

    repo_id: str
    display_name: str
    repo_path: Path
    config: AppConfig


class PublishFailureCategory(Enum):
    """Categorized publish failure reason for recovery context."""

    PUSH = "push"
    PR_LOOKUP = "pr_lookup"
    PR_CREATE = "pr_create"
    LABEL_UPDATE = "label_update"
    COMMENT_UPDATE = "comment_update"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PublishRecoveryRequest:
    """Request for publish recovery."""

    issue_number: int
    expected_branch: str | None = None


@dataclass(frozen=True)
class PublishRecoveryResult:
    """Result of successful publish recovery."""

    issue_number: int
    branch: str
    head_sha: str
    pr_url: str
    pr_reused: bool
    supervisor_action: str = ""


# ---------------------------------------------------------------------------
# Dependency gate models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryDependencyDeclaration:
    """Structured dependency declaration parsed from a PRD.

    Attributes:
        group: The task group this Issue belongs to, or empty string.
        depends_on_groups: Group labels that must be fully closed.
        depends_on_issues: Specific Issue numbers that must be closed.
        depends_on_prds: PRD paths or filenames to resolve at Issue creation time.
        gate_type: One of ``"hard"``, ``"soft"``, ``"none"``.
        notes: Free-form operator notes.
    """

    group: str = ""
    depends_on_groups: tuple[str, ...] = ()
    depends_on_issues: tuple[int, ...] = ()
    depends_on_prds: tuple[str, ...] = ()
    gate_type: str = "none"
    notes: str = ""


@dataclass(frozen=True)
class DependencyDeclaration:
    """Materialised dependency declaration from an Issue body.

    Attributes:
        issue_numbers: Upstream Issue numbers this Issue depends on.
        groups: Upstream group labels this Issue depends on.
    """

    issue_numbers: tuple[int, ...] = ()
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class DependencyBlocker:
    """A single unsatisfied dependency."""

    blocker_type: str
    target: str
    current_state: str


@dataclass(frozen=True)
class DependencyVerdict:
    """Result of evaluating an Issue's dependencies."""

    satisfied: bool
    blockers: tuple[DependencyBlocker, ...] = ()
    has_failed_or_blocked_upstream: bool = False
    empty_group_names: tuple[str, ...] = ()

"""Agent Runner domain models and value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from backend.core.shared.models.agent_decision import InteractiveDecisionConfig


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
    UNRECOVERABLE = "unrecoverable"
    FORBIDDEN_BLOCKED = "forbidden_blocked"


@dataclass(frozen=True)
class AttemptResult:
    """Record of a single agent execution attempt."""

    attempt_number: int
    failure_type: FailureType
    recovered: bool
    detail: str


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
    """Local runner behavior."""

    max_issues: int = 1
    default_agent: str = "auto"
    max_recovery_attempts: int = 5
    recovery_retry_delay_seconds: int = 30
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
    """

    enabled: bool = True
    evidence_dir: str = ".iar/evidence"
    branch_prefix: str = "iar-evidence/"
    evidence_format_check: bool = True
    parse_evidence_format_with_agent: bool = True


@dataclass(frozen=True)
class PrePushReviewConfig:
    """Pre-push AI review gate configuration."""

    enabled: bool = True
    review_agent: str = "auto"
    allow_same_agent: bool = True
    max_attempts: int = 2
    timeout_seconds: int = 900


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

    enabled: bool = False
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

    enabled: bool = False
    fallback: str = "template"
    max_input_chars: int = 20000
    default_agent: str = "auto"
    issue_from_prd: GeneratedContentTargetConfig = field(
        default_factory=GeneratedContentTargetConfig
    )
    draft_pr: GeneratedContentTargetConfig = field(
        default_factory=GeneratedContentTargetConfig
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
    pre_push_review: PrePushReviewConfig = PrePushReviewConfig()
    post_pr_supervisor: PostPrSupervisorConfig = PostPrSupervisorConfig()
    generated_content: GeneratedContentConfig = field(
        default_factory=GeneratedContentConfig
    )
    interactive_decision: InteractiveDecisionConfig = field(
        default_factory=InteractiveDecisionConfig
    )


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

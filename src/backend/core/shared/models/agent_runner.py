"""Agent Runner domain models and value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


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


@dataclass(frozen=True)
class LabelConfig:
    """GitHub labels used as runner queue state."""

    ready: str = "agent/ready"
    running: str = "agent/running"
    supervising: str = "agent/supervising"
    review: str = "agent/review"
    failed: str = "agent/failed"
    blocked: str = "agent/blocked"
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
    """Commands used to create and locate target worktrees."""

    create_command: str = "just worktree issue-{issue_number} enter_shell=false"
    reuse_command: str = 'bash -c \'test -d "$(dirname "$(git rev-parse --show-toplevel)")/$(basename "$(git rev-parse --show-toplevel)")-worktrees/tasks/issue-{issue_number}"\''
    path_command: str = 'bash -c \'echo "$(dirname "$(git rev-parse --show-toplevel)")/$(basename "$(git rev-parse --show-toplevel)")-worktrees/tasks/issue-{issue_number}"\''


@dataclass(frozen=True)
class RunnerConfig:
    """Local runner behavior."""

    max_issues: int = 1
    default_agent: str = "auto"
    max_recovery_attempts: int = 2
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
class PrePushReviewConfig:
    """Pre-push AI review gate configuration."""

    enabled: bool = True
    review_agent: str = "auto"
    allow_same_agent: bool = True
    max_attempts: int = 2


@dataclass(frozen=True)
class PostPrSupervisorConfig:
    """Post-PR supervisor cycle configuration."""

    enabled: bool = True
    supervisor_agent: str = "auto"
    max_repair_attempts: int = 2


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
    timeout_seconds: int = 60
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
    prompts: PromptConfig = field(default_factory=PromptConfig)
    pre_push_review: PrePushReviewConfig = PrePushReviewConfig()
    post_pr_supervisor: PostPrSupervisorConfig = PostPrSupervisorConfig()
    generated_content: GeneratedContentConfig = field(
        default_factory=GeneratedContentConfig
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

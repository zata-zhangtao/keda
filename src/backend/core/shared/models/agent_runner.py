"""Agent Runner domain models and value objects."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CommandResult:
    """Captured subprocess result."""

    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str


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
    reuse_command: str = 'bash -c \'test -d "$(dirname "$(git rev-parse --show-toplevel)")/issue-{issue_number}"\''
    path_command: str = 'bash -c \'echo "$(dirname "$(git rev-parse --show-toplevel)")/issue-{issue_number}"\''


@dataclass(frozen=True)
class RunnerConfig:
    """Local runner behavior."""

    max_issues: int = 1
    default_agent: str = "auto"
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
class AppConfig:
    """Application configuration."""

    labels: LabelConfig = LabelConfig()
    git: GitConfig = GitConfig()
    worktree: WorktreeConfig = WorktreeConfig()
    runner: RunnerConfig = RunnerConfig()
    safety: SafetyConfig = SafetyConfig()

"""Agent Runner domain models and value objects."""

from __future__ import annotations

from dataclasses import dataclass


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
    codex: str = "agent/codex"
    claude: str = "agent/claude"


@dataclass(frozen=True)
class GitConfig:
    """Git publishing configuration."""

    remote: str = "origin"
    base_branch: str = "main"


@dataclass(frozen=True)
class WorktreeConfig:
    """Commands used to create and locate target worktrees."""

    create_command: str = "just worktree --issue {issue_number} enter_shell=false"
    reuse_command: str = "just worktree --issue {issue_number} --existing-branch enter_shell=false"
    path_command: str = "bash scripts/git_worktree.sh --print-path --issue {issue_number} --existing-branch"


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

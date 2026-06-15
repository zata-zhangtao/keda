// Shared API types used by the frontend.
// Keep these aligned with the backend dataclasses under
// `src/backend/core/use_cases/agent_runner_monitor.py`.

export type UserSession = {
  user_id: string;
  display_name: string;
  email: string;
};

// ─────────────────────────────────────────────────────────────────────────────
// Agent Runner Monitoring Dashboard
// ─────────────────────────────────────────────────────────────────────────────

export type AnomalySeverity = "warning" | "error";

export type AnomalyType =
  | "label_pr_mismatch"
  | "pr_dirty_in_review"
  | "dirty_worktree_mismatch"
  | "event_label_mismatch";

export type Anomaly = {
  type: AnomalyType;
  severity: AnomalySeverity;
  message: string;
  suggested_cli: string[];
};

export type QueueLabels = {
  ready: string;
  running: string;
  supervising: string;
  review: string;
  failed: string;
  blocked: string;
};

export type RepositoryHealth = {
  gh_available: boolean;
  repo_path_exists: boolean;
  publish_remote_exists: boolean;
};

export type WorktreeStatus = {
  exists: boolean;
  path: string;
  branch: string;
  head_sha: string;
  is_clean: boolean;
  dirty_files: string[];
};

export type PullRequestContext = {
  number: number | null;
  url: string;
  branch: string;
  head_sha: string;
  base_sha: string;
  mergeable: boolean | null;
  checks_state: string | null;
  checks_summary: string[];
};

export type EventTimelineEntry = {
  phase: string;
  cycle: number;
  comment_index: number;
  action: string | null;
  head_sha: string | null;
  pr_branch: string | null;
  checks_state: string | null;
  mergeable: boolean | null;
  raw_marker: string;
};

export type LatestEventMarker = {
  version: number;
  phase: string;
  cycle: number;
  head_sha: string | null;
  base_sha: string | null;
  pr_branch: string | null;
  action: string | null;
  checks_state: string | null;
  mergeable: boolean | null;
  issue_comments_count: number | null;
  pr_comments_count: number | null;
};

export type IssueMonitoringSnapshot = {
  number: number;
  title: string;
  url: string;
  labels: string[];
  state: string;
  primary_label: string;
  pr: PullRequestContext | null;
  worktree: WorktreeStatus;
  timeline: EventTimelineEntry[];
  latest_event: LatestEventMarker | null;
  anomalies: Anomaly[];
  suggested_cli_commands: string[];
  has_anomaly: boolean;
  anomaly_types: AnomalyType[];
};

export type QueueCounts = {
  ready: number;
  running: number;
  supervising: number;
  review: number;
  failed: number;
  blocked: number;
};

export type AnomalySummary = {
  warning: number;
  error: number;
};

export type RepositoryMonitoringOverview = {
  repo_id: string;
  display_name: string;
  enabled: boolean;
  base_branch: string;
  remote: string;
  health: RepositoryHealth;
  queue_counts: QueueCounts;
  labels: QueueLabels;
  issues: IssueMonitoringSnapshot[];
  anomaly_count: number;
  anomaly_summary: AnomalySummary;
  scanned_at: string;
};

export type MonitoringOverview = {
  repositories: RepositoryMonitoringOverview[];
  scanned_at: string;
  unreachable_repositories?: UnreachableRepository[];
};

// ─────────────────────────────────────────────────────────────────────────────
// Agent Runner Operations Console
// Keep these aligned with the backend dataclasses under
// `src/backend/core/shared/interfaces/runner_console.py`.
// ─────────────────────────────────────────────────────────────────────────────

export type RunnerProcessKind =
  | "daemon"
  | "review_daemon"
  | "run_once"
  | "review_once"
  | "blocked_continue";

export type RunnerProcessStatus = "running" | "exited" | "stopped" | "killed";

export type RunnerProcessRecord = {
  process_id: string;
  repo_id: string;
  kind: RunnerProcessKind;
  pid: number;
  status: RunnerProcessStatus;
  exit_code: number | null;
  log_path: string;
  command: string[];
  started_at: string;
  stopped_at: string | null;
};

export type ProcessLogChunk = {
  content: string;
  next_offset: number;
  eof: boolean;
};

export type ConsoleActionResult = {
  action: string;
  result: "accepted" | "rejected" | "error";
  detail: string;
  process: RunnerProcessRecord | null;
};

export type RepositoryCompletionStats = {
  repo_id: string;
  display_name: string;
  total_tracked: number;
  completed: number;
  failed: number;
  blocked: number;
  open_in_pipeline: number;
  completion_rate: number | null;
  truncated: boolean;
  error: string | null;
};

export type DailyRunTrendEntry = {
  day: string;
  completed: number;
  failed: number;
  blocked: number;
  average_duration_seconds: number | null;
};

export type RunRecordEntry = {
  repo_id: string;
  repo_path: string;
  issue_number: number;
  trigger: string;
  agent: string;
  outcome: "completed" | "failed" | "blocked";
  error_summary: string | null;
  started_at: string;
  finished_at: string;
  duration_seconds: number;
};

export type AuditEntry = {
  occurred_at: string;
  actor: string;
  action: string;
  repo_id: string | null;
  issue_number: number | null;
  params_json: string;
  result: "accepted" | "rejected" | "error";
  detail: string | null;
};

export type RegistryRepositoryEntry = {
  repo_id: string;
  path: string;
  enabled: boolean;
  display_name: string | null;
  path_exists: boolean;
};

export type UnreachableRepository = {
  repo_id: string;
  display_name: string;
  configured_path: string;
  error: string;
};

// ─────────────────────────────────────────────────────────────────────────────
// Roadmap
// Keep these aligned with the backend dataclasses under
// `src/backend/core/shared/models/roadmap.py`.
// ─────────────────────────────────────────────────────────────────────────────

export type RoadmapPrdState =
  | "not_started"
  | "ready"
  | "running"
  | "supervising"
  | "review"
  | "failed"
  | "blocked"
  | "merged"
  | "archived"
  | "unresolved_dependency"
  | "waiting";

export type RoadmapDependencyKind = "prd" | "issue" | "group" | "unresolved";

export type RoadmapDependency = {
  from_path: string;
  to_path: string;
  kind: RoadmapDependencyKind;
  detail: string;
};

export type RoadmapNextAction = {
  label: string;
  url: string | null;
};

export type RoadmapPrd = {
  prd_path: string;
  title: string;
  status: "pending" | "archived";
  priority: string;
  issue_url: string | null;
  issue_number: number | null;
  state: RoadmapPrdState;
  acceptance_total: number;
  acceptance_checked: number;
  delivery_dependencies: RoadmapDependency[];
  updated_at: string;
  block_reason: string | null;
  next_action: RoadmapNextAction | null;
};

export type RoadmapSettings = {
  repo_id: string;
  max_parallel: number;
  default_view: "timeline" | "list";
  updated_at: string;
};

export type RoadmapActionResult = {
  prd_path: string;
  issue_number: number | null;
  state: RoadmapPrdState;
  detail: string;
};

export type RoadmapGlobalStartResult = {
  started: RoadmapActionResult[];
  queued: string[];
  skipped: string[];
};

// ─────────────────────────────────────────────────────────────────────────────
// Idea Inbox
// Keep these aligned with the backend dataclasses under
// `src/backend/core/shared/models/idea_inbox.py`.
// ─────────────────────────────────────────────────────────────────────────────

export type IdeaInboxSource = "frontend" | "inbound" | "feishu" | "manual";

export type PrdDraftStatus = "pending-review" | "approved" | "rejected";

export type IdeaEntry = {
  entry_id: string;
  occurred_at: string;
  source: IdeaInboxSource;
  author: string;
  text: string;
};

export type PrdDraftMetadata = {
  draft_id: string;
  status: PrdDraftStatus;
  repo_id: string;
  source_idea_refs: string[];
  priority: string;
  prd_type: string;
  created_at: string;
  approved_pending_path: string | null;
};

export type PrdDraftSummary = {
  metadata: PrdDraftMetadata;
  draft_path: string;
  title: string;
  body_excerpt: string;
};

export type IdeaInboxSnapshot = {
  repo_id: string;
  ideas_path: string;
  summary_path: string;
  drafts_dir: string;
  ideas_raw: string;
  summary_raw: string;
  entries: IdeaEntry[];
  drafts: PrdDraftSummary[];
};

export type AppendIdeaResponse = {
  entry: IdeaEntry;
  ideas_path: string;
};

export type RefreshSummaryResponse = {
  summary_path: string;
  summary_text: string;
  source: string;
};

export type IdeaInboxMetadata = {
  priorities: string[];
  prd_types: string[];
  inbound_signature_header: string;
  inbound_secret_env: string;
};

export type CreateDraftResponse = {
  draft: PrdDraftSummary;
  draft_path: string;
};

export type ApproveDraftResponse = {
  draft: PrdDraftSummary;
  pending_path: string;
};

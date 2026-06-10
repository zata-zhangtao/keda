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
};

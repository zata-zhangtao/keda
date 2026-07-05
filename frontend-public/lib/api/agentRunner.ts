// Read-only Agent Runner monitoring API wrapper.
//
// This module is the only place the dashboard talks to `/api/v1/agent-runner/*`.
// It must never reach into GitHub or Git directly — keep that boundary enforced
// so the panel stays a monitoring surface, not a recovery surface.

import { get } from "./client";
import type {
  IssueMonitoringSnapshot,
  MonitoringOverview,
} from "./types";

const BASE_PATH = "/v1/agent-runner";

/** Parameters accepted by {@link fetchMonitoringOverview}. */
export type FetchMonitoringOverviewParams = {
  /** Optional whitelist of repository IDs to scope the overview to. */
  repoIds?: string[];
  /** When true, run the build asynchronously and return a job id instead of the payload. */
  asyncRun?: boolean;
};

/** Async-mode acknowledgement returned by the overview endpoint. */
export type OverviewJobHandle = {
  async: true;
  job_id: string;
  repo_ids: string[] | null;
};

/** Status of an async overview build. */
export type OverviewJobStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed";

/** Snapshot of an overview job returned by the polling endpoint. */
export type OverviewJobSnapshot = {
  job_id: string;
  status: OverviewJobStatus;
  repo_ids: string[] | null;
  created_at: number | null;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  payload: MonitoringOverview | null;
};

/** Mapping of repository id → job id returned by the per-repo endpoint. */
export type OverviewJobsByRepo = {
  jobs_by_repo: Record<string, string>;
  started_at: number;
};

function buildOverviewQuery(params: FetchMonitoringOverviewParams): string {
  const searchParams = new URLSearchParams();
  if (params.repoIds && params.repoIds.length > 0) {
    searchParams.set("repo_ids", params.repoIds.join(","));
  }
  if (params.asyncRun) {
    searchParams.set("async_run", "true");
  }
  const queryString = searchParams.toString();
  return queryString ? `?${queryString}` : "";
}

/** Fetch the monitoring overview. */
export async function fetchMonitoringOverview(
  params: FetchMonitoringOverviewParams = {},
): Promise<MonitoringOverview | OverviewJobHandle> {
  return get<MonitoringOverview | OverviewJobHandle>(
    `${BASE_PATH}/overview${buildOverviewQuery(params)}`,
  );
}

/** Poll an async overview job until it reaches a terminal state. */
export async function pollOverviewJob(
  jobId: string,
  options: { intervalMs?: number; signal?: AbortSignal } = {},
): Promise<OverviewJobSnapshot> {
  const intervalMs = options.intervalMs ?? 5000;
  while (true) {
    if (options.signal?.aborted) {
      throw new DOMException("Overview polling aborted", "AbortError");
    }
    const snapshot = await get<OverviewJobSnapshot>(
      `${BASE_PATH}/overview/jobs/${encodeURIComponent(jobId)}`,
    );
    if (snapshot.status === "completed" || snapshot.status === "failed") {
      return snapshot;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

export async function fetchIssueDetail(
  issueNumber: number,
): Promise<IssueMonitoringSnapshot> {
  return get<IssueMonitoringSnapshot>(`${BASE_PATH}/issues/${issueNumber}`);
}

/**
 * Start one overview job per enabled repository and return the repo→job_id
 * mapping. The dashboard polls each job independently to render cards as
 * their scans complete (gradual reveal).
 */
export async function fetchOverviewJobsByRepo(): Promise<OverviewJobsByRepo> {
  return get<OverviewJobsByRepo>(`${BASE_PATH}/overview/per-repo`);
}

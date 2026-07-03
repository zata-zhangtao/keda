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

export async function fetchMonitoringOverview(): Promise<MonitoringOverview> {
  return get<MonitoringOverview>(`${BASE_PATH}/overview`);
}

export async function fetchIssueDetail(
  issueNumber: number,
): Promise<IssueMonitoringSnapshot> {
  return get<IssueMonitoringSnapshot>(`${BASE_PATH}/issues/${issueNumber}`);
}

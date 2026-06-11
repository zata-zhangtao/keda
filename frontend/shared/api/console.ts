// Agent Runner Operations Console API wrapper.
//
// This module is the only place the console pages talk to
// `/api/v1/agent-runner/console/*` and `/api/v1/agent-runner/repositories*`.
// All write operations map to backend whitelisted actions; the frontend
// never sends raw shell commands.

import { get, patch, post } from "@shared/api/client";
import type {
  AuditEntry,
  ConsoleActionResult,
  DailyRunTrendEntry,
  ProcessLogChunk,
  RegistryRepositoryEntry,
  RepositoryCompletionStats,
  RunnerProcessKind,
  RunnerProcessRecord,
  RunRecordEntry,
} from "@shared/api/types";

const BASE_PATH = "/v1/agent-runner";

// ── 托管进程 ────────────────────────────────────────────────────────────────

export async function fetchProcesses(): Promise<RunnerProcessRecord[]> {
  const response = await get<{ processes: RunnerProcessRecord[] }>(
    `${BASE_PATH}/console/processes`,
  );
  return response.processes;
}

export async function startProcess(params: {
  repo_id: string;
  kind: RunnerProcessKind;
}): Promise<RunnerProcessRecord> {
  return post<RunnerProcessRecord>(`${BASE_PATH}/console/processes`, params);
}

export async function stopProcess(
  processId: string,
): Promise<RunnerProcessRecord> {
  return post<RunnerProcessRecord>(
    `${BASE_PATH}/console/processes/${processId}/stop`,
  );
}

export async function fetchProcessLog(
  processId: string,
  offset: number,
): Promise<ProcessLogChunk> {
  return get<ProcessLogChunk>(
    `${BASE_PATH}/console/processes/${processId}/logs?offset=${offset}`,
  );
}

// ── 白名单动作 ──────────────────────────────────────────────────────────────

export async function executeRepositoryAction(
  repoId: string,
  action: "run_once" | "review_once",
): Promise<ConsoleActionResult> {
  return post<ConsoleActionResult>(
    `${BASE_PATH}/console/repositories/${encodeURIComponent(repoId)}/actions`,
    { action },
  );
}

export async function executeIssueAction(
  repoId: string,
  issueNumber: number,
  action: "retry_failed" | "blocked_continue",
): Promise<ConsoleActionResult> {
  return post<ConsoleActionResult>(
    `${BASE_PATH}/console/repositories/${encodeURIComponent(repoId)}/issues/${issueNumber}/actions`,
    { action },
  );
}

// ── 统计 / 历史 / 审计 ──────────────────────────────────────────────────────

export async function fetchCompletionStats(): Promise<
  RepositoryCompletionStats[]
> {
  const response = await get<{ repositories: RepositoryCompletionStats[] }>(
    `${BASE_PATH}/console/stats/overview`,
  );
  return response.repositories;
}

export async function fetchRunHistoryTrend(params: {
  repoId?: string;
  days?: number;
}): Promise<DailyRunTrendEntry[]> {
  const searchParams = new URLSearchParams();
  if (params.repoId) {
    searchParams.set("repo_id", params.repoId);
  }
  searchParams.set("days", String(params.days ?? 30));
  const response = await get<{ trend: DailyRunTrendEntry[] }>(
    `${BASE_PATH}/console/stats/history?${searchParams.toString()}`,
  );
  return response.trend;
}

export async function fetchRecentRuns(params: {
  repoId?: string;
  limit?: number;
}): Promise<RunRecordEntry[]> {
  const searchParams = new URLSearchParams();
  if (params.repoId) {
    searchParams.set("repo_id", params.repoId);
  }
  searchParams.set("limit", String(params.limit ?? 50));
  const response = await get<{ runs: RunRecordEntry[] }>(
    `${BASE_PATH}/console/runs?${searchParams.toString()}`,
  );
  return response.runs;
}

export async function fetchAuditLog(limit = 100): Promise<AuditEntry[]> {
  const response = await get<{ audits: AuditEntry[] }>(
    `${BASE_PATH}/console/audit?limit=${limit}`,
  );
  return response.audits;
}

// ── 仓库 registry 管理 ──────────────────────────────────────────────────────

export async function fetchRegistryRepositories(): Promise<
  RegistryRepositoryEntry[]
> {
  const response = await get<{ repositories: RegistryRepositoryEntry[] }>(
    `${BASE_PATH}/repositories`,
  );
  return response.repositories;
}

export async function addRegistryRepository(params: {
  repo_id: string;
  path: string;
  display_name?: string;
}): Promise<RegistryRepositoryEntry> {
  return post<RegistryRepositoryEntry>(`${BASE_PATH}/repositories`, params);
}

export async function setRegistryRepositoryEnabled(
  repoId: string,
  enabled: boolean,
): Promise<void> {
  await patch(`${BASE_PATH}/repositories/${encodeURIComponent(repoId)}`, {
    enabled,
  });
}

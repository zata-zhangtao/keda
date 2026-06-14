// Roadmap API wrapper for `/api/v1/agent-runner/roadmap/*`.

import { get, patch, post } from "@shared/api/client";
import type {
  RoadmapGlobalStartResult,
  RoadmapPrd,
  RoadmapSettings,
  RoadmapActionResult,
} from "@shared/api/types";

const BASE_PATH = "/v1/agent-runner/roadmap";

export async function fetchRoadmapPrds(params: {
  repoId: string;
  includeArchived?: boolean;
}): Promise<{ prds: RoadmapPrd[]; repo_id: string; include_archived: boolean; scanned_at: string }> {
  const searchParams = new URLSearchParams();
  searchParams.set("repo_id", params.repoId);
  if (params.includeArchived) {
    searchParams.set("include_archived", "true");
  }
  return get(`${BASE_PATH}/prds?${searchParams.toString()}`);
}

export async function fetchRoadmapSettings(repoId: string): Promise<RoadmapSettings> {
  return get(`${BASE_PATH}/settings?repo_id=${encodeURIComponent(repoId)}`);
}

export async function updateRoadmapSettings(params: {
  repoId: string;
  maxParallel: number;
  defaultView: "timeline" | "list";
}): Promise<RoadmapSettings> {
  return patch(`${BASE_PATH}/settings?repo_id=${encodeURIComponent(params.repoId)}`, {
    max_parallel: params.maxParallel,
    default_view: params.defaultView,
  });
}

export async function startRoadmapPrd(repoId: string, prdPath: string): Promise<RoadmapActionResult> {
  const encodedPath = btoa(prdPath)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
  return post(`${BASE_PATH}/prds/${encodedPath}/start`, { repo_id: repoId });
}

export async function startGlobalRoadmap(params: {
  repoId: string;
  maxParallel: number;
}): Promise<RoadmapGlobalStartResult> {
  return post(`${BASE_PATH}/start-global`, {
    repo_id: params.repoId,
    max_parallel: params.maxParallel,
  });
}

export async function stopGlobalRoadmap(repoId: string): Promise<{ stopped: boolean; repo_id: string }> {
  return post(`${BASE_PATH}/stop-global`, { repo_id: repoId });
}

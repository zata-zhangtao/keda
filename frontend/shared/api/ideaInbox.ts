// Idea Inbox API wrapper for `/api/v1/agent-runner/idea-inbox/*`.

import { get, post } from "@shared/api/client";
import type {
  AppendIdeaResponse,
  ApproveDraftResponse,
  CreateDraftResponse,
  IdeaEntry,
  IdeaInboxMetadata,
  IdeaInboxSnapshot,
  PrdDraftSummary,
  RefreshSummaryResponse,
} from "@shared/api/types";

const BASE_PATH = "/v1/agent-runner/idea-inbox";

export async function fetchIdeaInboxSnapshot(
  repoId: string,
): Promise<IdeaInboxSnapshot> {
  return get<IdeaInboxSnapshot>(
    `${BASE_PATH}/repositories/${encodeURIComponent(repoId)}`,
  );
}

export async function appendIdea(params: {
  repoId: string;
  text: string;
  author?: string;
  occurredAt?: string;
}): Promise<AppendIdeaResponse> {
  return post<AppendIdeaResponse>(
    `${BASE_PATH}/repositories/${encodeURIComponent(params.repoId)}/ideas`,
    {
      text: params.text,
      author: params.author ?? "anonymous",
      ...(params.occurredAt ? { occurred_at: params.occurredAt } : {}),
    },
  );
}

export async function refreshIdeaSummary(params: {
  repoId: string;
  summaryText: string;
  sourceLabel?: string;
}): Promise<RefreshSummaryResponse> {
  return post<RefreshSummaryResponse>(
    `${BASE_PATH}/repositories/${encodeURIComponent(params.repoId)}/summary/refresh`,
    {
      summary_text: params.summaryText,
      source_label: params.sourceLabel ?? "agent",
    },
  );
}

export async function createPrdDraft(params: {
  repoId: string;
  ideaRefs: string[];
  priority?: string;
  prdType?: string;
  agentName?: string;
  timeoutSeconds?: number;
}): Promise<CreateDraftResponse> {
  return post<CreateDraftResponse>(
    `${BASE_PATH}/repositories/${encodeURIComponent(params.repoId)}/drafts`,
    {
      idea_refs: params.ideaRefs,
      priority: params.priority ?? "P2",
      prd_type: params.prdType ?? "FEAT",
      agent_name: params.agentName ?? "codex",
      timeout_seconds: params.timeoutSeconds ?? 600,
    },
  );
}

export async function approvePrdDraft(params: {
  repoId: string;
  draftPath: string;
  priority?: string;
  prdType?: string;
}): Promise<ApproveDraftResponse> {
  const encodedPath = encodeDraftPath(params.draftPath);
  return post<ApproveDraftResponse>(
    `${BASE_PATH}/repositories/${encodeURIComponent(params.repoId)}/drafts/${encodedPath}/approve`,
    {
      ...(params.priority ? { priority: params.priority } : {}),
      ...(params.prdType ? { prd_type: params.prdType } : {}),
    },
  );
}

export async function fetchIdeaInboxMetadata(): Promise<IdeaInboxMetadata> {
  return get<IdeaInboxMetadata>(`${BASE_PATH}/metadata`);
}

function encodeDraftPath(draftPath: string): string {
  const bytes = new TextEncoder().encode(draftPath);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_");
}

export type {
  IdeaEntry,
  IdeaInboxSnapshot,
  PrdDraftSummary,
};

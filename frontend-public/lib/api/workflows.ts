import { apiDelete, apiGet, apiPost, apiPut } from "./client"
import type {
  Workflow,
  WorkflowCreatePayload,
  WorkflowUpdatePayload,
} from "@/lib/types/workflow"

/** List all workflows. */
export async function listWorkflows(): Promise<Workflow[]> {
  return apiGet<Workflow[]>("/workflows")
}

/** Get a single workflow by ID. */
export async function getWorkflow(workflowId: string): Promise<Workflow> {
  return apiGet<Workflow>(`/workflows/${workflowId}`)
}

/** Create a new workflow. */
export async function createWorkflow(
  payload: WorkflowCreatePayload
): Promise<Workflow> {
  return apiPost<Workflow>("/workflows", payload)
}

/** Update an existing workflow. */
export async function updateWorkflow(
  workflowId: string,
  payload: WorkflowUpdatePayload
): Promise<Workflow> {
  return apiPut<Workflow>(`/workflows/${workflowId}`, payload)
}

/** Delete a workflow by ID. */
export async function deleteWorkflow(workflowId: string): Promise<void> {
  return apiDelete(`/workflows/${workflowId}`)
}

/** Trigger a workflow run by ID. */
export async function runWorkflow(workflowId: string): Promise<unknown> {
  return apiPost<unknown>(`/workflows/${workflowId}/run`, {})
}

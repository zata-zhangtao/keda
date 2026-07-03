import { apiDelete, apiGet, apiPost, apiPut } from "./client"
import type {
  Agent,
  AgentCreatePayload,
  AgentUpdatePayload,
} from "@/lib/types/agent"

/** List all agents. */
export async function listAgents(): Promise<Agent[]> {
  return apiGet<Agent[]>("/agents")
}

/** Get a single agent by ID. */
export async function getAgent(agentId: string): Promise<Agent> {
  return apiGet<Agent>(`/agents/${agentId}`)
}

/** Create a new agent. */
export async function createAgent(payload: AgentCreatePayload): Promise<Agent> {
  return apiPost<Agent>("/agents", payload)
}

/** Update an existing agent. */
export async function updateAgent(
  agentId: string,
  payload: AgentUpdatePayload
): Promise<Agent> {
  return apiPut<Agent>(`/agents/${agentId}`, payload)
}

/** Delete an agent by ID. */
export async function deleteAgent(agentId: string): Promise<void> {
  return apiDelete(`/agents/${agentId}`)
}

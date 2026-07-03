export type AgentStatus = "idle" | "running" | "error" | "offline"

export interface Agent {
  id: string
  owner_id: string
  name: string
  description: string
  system_prompt: string
  model: string
  tool_ids: string[]
  status: AgentStatus
  created_at?: string
  updated_at?: string
}

export interface AgentCreatePayload {
  name: string
  description?: string
  system_prompt: string
  model: string
  tool_ids?: string[]
}

export interface AgentUpdatePayload extends AgentCreatePayload {
  status?: AgentStatus
}

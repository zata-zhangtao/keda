export interface ToolCall {
  id: string
  tool_name: string
  arguments: Record<string, unknown>
  result?: unknown
  status: "pending" | "running" | "success" | "error"
}

export interface ChatMessage {
  id: string
  session_id: string
  role: "user" | "assistant" | "system"
  content: string
  tool_calls: ToolCall[]
  created_at?: string
}

export interface ChatSession {
  id: string
  owner_id: string
  agent_id: string
  title: string
  created_at?: string
  updated_at?: string
}

export interface ChatSessionCreatePayload {
  agent_id: string
  title?: string
}

export interface ChatMessageCreatePayload {
  content: string
}

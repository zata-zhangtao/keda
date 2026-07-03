import { apiDelete, apiGet, apiPost } from "./client"
import type {
  ChatMessage,
  ChatMessageCreatePayload,
  ChatSession,
  ChatSessionCreatePayload,
} from "@/lib/types/session"

/** List all chat sessions. */
export async function listSessions(): Promise<ChatSession[]> {
  return apiGet<ChatSession[]>("/sessions")
}

/** Get a single chat session by ID. */
export async function getSession(sessionId: string): Promise<ChatSession> {
  return apiGet<ChatSession>(`/sessions/${sessionId}`)
}

/** Create a new chat session. */
export async function createSession(
  payload: ChatSessionCreatePayload
): Promise<ChatSession> {
  return apiPost<ChatSession>("/sessions", payload)
}

/** Delete a chat session by ID. */
export async function deleteSession(sessionId: string): Promise<void> {
  return apiDelete(`/sessions/${sessionId}`)
}

/** List messages in a chat session. */
export async function listMessages(sessionId: string): Promise<ChatMessage[]> {
  return apiGet<ChatMessage[]>(`/sessions/${sessionId}/messages`)
}

/** Send a message in a chat session. */
export async function sendMessage(
  sessionId: string,
  payload: ChatMessageCreatePayload
): Promise<ChatMessage> {
  return apiPost<ChatMessage>(`/sessions/${sessionId}/messages`, payload)
}

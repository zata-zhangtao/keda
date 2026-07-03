import { apiGet } from "./client"
import type { ToolDefinition } from "@/lib/types/tool"

/** List all available tool definitions. */
export async function listTools(): Promise<ToolDefinition[]> {
  return apiGet<ToolDefinition[]>("/tools")
}

export interface ToolDefinition {
  id: string
  name: string
  description: string
  parameters_schema: Record<string, unknown>
}

export interface WorkflowNode {
  id?: string
  node_type: string
  label: string
  config?: Record<string, unknown>
  position_x?: number
  position_y?: number
}

export interface WorkflowEdge {
  id?: string
  source_node_id: string
  target_node_id: string
}

export interface Workflow {
  id: string
  owner_id: string
  name: string
  description: string
  status: string
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  created_at?: string
  updated_at?: string
}

export interface WorkflowCreatePayload {
  name: string
  description?: string
}

export interface WorkflowUpdatePayload {
  name: string
  description?: string
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  status?: string
}

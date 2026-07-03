"use client"

import { useMemo } from "react"
import { WorkflowCanvas } from "@/components/workflow/workflow-canvas"
import type { Workflow } from "@/lib/types/workflow"

interface WorkflowViewerProps {
  workflow: Workflow
  readOnly?: boolean
}

/** Read-only or editable workflow diagram viewer. */
export function WorkflowViewer({ workflow, readOnly = true }: WorkflowViewerProps) {
  const initialNodes = useMemo(
    () =>
      workflow.nodes.map((node, index) => ({
        id: node.id || `node-${index}`,
        type: "custom" as const,
        position: { x: node.position_x || 0, y: node.position_y || 0 },
        data: {
          nodeType: node.node_type,
          label: node.label,
          config: node.config || {},
        },
      })),
    [workflow.nodes]
  )

  const initialEdges = useMemo(
    () =>
      workflow.edges.map((edge, index) => ({
        id: edge.id || `edge-${index}`,
        source: edge.source_node_id,
        target: edge.target_node_id,
      })),
    [workflow.edges]
  )

  return (
    <WorkflowCanvas
      initialNodes={initialNodes}
      initialEdges={initialEdges}
      readOnly={readOnly}
    />
  )
}

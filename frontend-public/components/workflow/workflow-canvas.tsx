"use client"

import { useCallback, useEffect } from "react"
import {
  addEdge,
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { WorkflowNodeCard, type CustomNodeData } from "./workflow-node"
import { WorkflowToolbar } from "./workflow-toolbar"

const nodeTypes = {
  custom: WorkflowNodeCard,
} as const

interface WorkflowCanvasProps {
  initialNodes?: Node<CustomNodeData>[]
  initialEdges?: Edge[]
  onChange?: (nodes: Node<CustomNodeData>[], edges: Edge[]) => void
  readOnly?: boolean
}

/** Interactive canvas for building and viewing workflows. */
export function WorkflowCanvas({
  initialNodes = [],
  initialEdges = [],
  onChange,
  readOnly = false,
}: WorkflowCanvasProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<CustomNodeData>>(initialNodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges)

  useEffect(() => {
    onChange?.(nodes, edges)
  }, [nodes, edges, onChange])

  const onConnect = useCallback(
    (connection: Connection) => {
      if (readOnly) return
      setEdges((eds) => addEdge({ ...connection, animated: true }, eds))
    },
    [readOnly, setEdges]
  )

  const handleAddNode = useCallback(
    (nodeType: string, label: string) => {
      if (readOnly) return
      setNodes((nds) => {
        const newNode: Node<CustomNodeData> = {
          id: `node-${Date.now()}`,
          type: "custom",
          position: { x: 100 + nds.length * 40, y: 100 + nds.length * 40 },
          data: { nodeType, label, config: {} },
        }
        return [...nds, newNode]
      })
    },
    [readOnly, setNodes]
  )

  return (
    <div className="flex h-[600px] flex-col rounded-2xl border">
      {!readOnly && <WorkflowToolbar onAddNode={handleAddNode} />}
      <div className="relative flex-1">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={readOnly ? undefined : onNodesChange}
          onEdgesChange={readOnly ? undefined : onEdgesChange}
          onConnect={readOnly ? undefined : onConnect}
          nodeTypes={nodeTypes}
          fitView
        >
          <Background />
          <MiniMap />
          <Controls />
        </ReactFlow>
      </div>
    </div>
  )
}

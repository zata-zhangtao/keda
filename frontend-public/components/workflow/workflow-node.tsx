"use client"

import { Handle, Position, type Node, type NodeProps } from "@xyflow/react"
import { Bot, GitBranch, Play, Square, Wrench } from "lucide-react"

const NODE_ICONS: Record<string, React.ElementType> = {
  start: Play,
  end: Square,
  agent: Bot,
  tool: Wrench,
  condition: GitBranch,
}

const NODE_COLORS: Record<string, string> = {
  start: "bg-emerald-500/10 text-emerald-500 border-emerald-500/20",
  end: "bg-rose-500/10 text-rose-500 border-rose-500/20",
  agent: "bg-primary/10 text-primary border-primary/20",
  tool: "bg-accent/10 text-accent border-accent/20",
  condition: "bg-amber-500/10 text-amber-500 border-amber-500/20",
}

export interface CustomNodeData extends Record<string, unknown> {
  nodeType: string
  label: string
  config?: Record<string, unknown>
}

export type CustomNode = Node<CustomNodeData>

/** Visual card for a workflow node on the canvas. */
export function WorkflowNodeCard({ data, selected }: NodeProps<CustomNode>) {
  const Icon = NODE_ICONS[data.nodeType] || Bot
  const colorClass = NODE_COLORS[data.nodeType] || NODE_COLORS.agent

  return (
    <div
      className={`min-w-[140px] rounded-xl border-2 bg-card p-3 shadow-sm ${
        selected ? "border-primary" : colorClass.split(" ")[2]
      } ${colorClass}`}
    >
      <Handle type="target" position={Position.Top} className="!bg-muted-foreground" />
      <div className="flex items-center gap-2">
        <Icon className="size-4" />
        <span className="text-sm font-medium">{data.label}</span>
      </div>
      {data.config && Object.keys(data.config).length > 0 && (
        <div className="mt-2 text-xs opacity-80">
          {Object.entries(data.config)
            .map(([key, value]) => `${key}: ${String(value)}`)
            .join(", ")}
        </div>
      )}
      <Handle
        type="source"
        position={Position.Bottom}
        className="!bg-muted-foreground"
      />
    </div>
  )
}

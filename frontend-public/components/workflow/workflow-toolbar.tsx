"use client"

import { Button } from "@/components/ui/button"
import { Bot, GitBranch, Play, Square, Wrench } from "lucide-react"

interface WorkflowToolbarProps {
  onAddNode: (nodeType: string, label: string) => void
}

const NODE_PRESETS = [
  { type: "start", label: "开始", icon: Play },
  { type: "agent", label: "Agent", icon: Bot },
  { type: "tool", label: "工具", icon: Wrench },
  { type: "condition", label: "条件", icon: GitBranch },
  { type: "end", label: "结束", icon: Square },
]

/** Toolbar with node presets for the workflow editor. */
export function WorkflowToolbar({ onAddNode }: WorkflowToolbarProps) {
  return (
    <div className="flex items-center gap-2 border-b p-3">
      <span className="mr-2 text-sm font-medium text-muted-foreground">添加节点:</span>
      {NODE_PRESETS.map((preset) => (
        <Button
          key={preset.type}
          size="sm"
          variant="outline"
          onClick={() => onAddNode(preset.type, preset.label)}
        >
          <preset.icon className="mr-1 size-4" />
          {preset.label}
        </Button>
      ))}
    </div>
  )
}

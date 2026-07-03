"use client"

import { useState } from "react"
import { cn } from "@/lib/utils"
import type { ToolCall } from "@/lib/types/session"
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react"

interface ToolCallCardProps {
  toolCall: ToolCall
}

/** Display a tool call with expandable details. */
export function ToolCallCard({ toolCall }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="rounded-lg border bg-muted/50 text-foreground">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-xs font-medium"
      >
        <span className="flex items-center gap-2">
          {toolCall.status === "running" && (
            <Loader2 className="size-3 animate-spin" />
          )}
          {toolCall.status === "success" && (
            <span className="size-2 rounded-full bg-emerald-500" />
          )}
          {toolCall.status === "error" && (
            <span className="size-2 rounded-full bg-rose-500" />
          )}
          工具调用: {toolCall.tool_name}
        </span>
        {expanded ? (
          <ChevronDown className="size-3" />
        ) : (
          <ChevronRight className="size-3" />
        )}
      </button>
      {expanded && (
        <div className="space-y-2 border-t px-3 py-2 text-xs">
          <div>
            <span className="font-medium">参数:</span>
            <pre className="mt-1 overflow-auto rounded bg-background p-2">
              {JSON.stringify(toolCall.arguments, null, 2)}
            </pre>
          </div>
          {toolCall.result !== undefined && (
            <div>
              <span className="font-medium">结果:</span>
              <pre
                className={cn(
                  "mt-1 overflow-auto rounded bg-background p-2",
                  toolCall.status === "error" && "text-rose-500"
                )}
              >
                {JSON.stringify(toolCall.result, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

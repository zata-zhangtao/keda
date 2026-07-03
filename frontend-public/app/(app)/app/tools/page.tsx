"use client"

import { useEffect, useState } from "react"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { listTools } from "@/lib/api/tools"
import type { ToolDefinition } from "@/lib/types/tool"
import { Loader2, Plug } from "lucide-react"
import { toast } from "sonner"

/** Render the tools page. */
export default function ToolsPage() {
  const [tools, setTools] = useState<ToolDefinition[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    listTools()
      .then(setTools)
      .catch(() => toast.error("加载工具失败"))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="size-8 animate-spin text-primary" />
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-3xl font-bold">工具</h1>
        <p className="text-muted-foreground">平台预置的可调用工具</p>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {tools.map((tool) => (
          <Card key={tool.id}>
            <CardHeader>
              <div className="flex items-center gap-3">
                <div className="flex size-10 items-center justify-center rounded-lg bg-primary/10">
                  <Plug className="size-5 text-primary" />
                </div>
                <div>
                  <CardTitle className="text-lg">{tool.name}</CardTitle>
                  <CardDescription className="text-xs">{tool.id}</CardDescription>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">{tool.description}</p>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

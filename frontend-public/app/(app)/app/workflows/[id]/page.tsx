"use client"

import Link from "next/link"
import { useEffect, useState } from "react"
import { useParams } from "next/navigation"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { WorkflowViewer } from "@/components/workflow/workflow-viewer"
import { getWorkflow, runWorkflow } from "@/lib/api/workflows"
import type { Workflow } from "@/lib/types/workflow"
import { GitBranch, Loader2, Play } from "lucide-react"
import { toast } from "sonner"

/** Render the workflowdetail page. */
export default function WorkflowDetailPage() {
  const params = useParams()
  const workflowId = params.id as string
  const [workflow, setWorkflow] = useState<Workflow | null>(null)
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)
  const [runResult, setRunResult] = useState<unknown>(null)

  useEffect(() => {
    getWorkflow(workflowId)
      .then(setWorkflow)
      .catch(() => toast.error("加载工作流失败"))
      .finally(() => setLoading(false))
  }, [workflowId])

  const handleRun = async () => {
    setRunning(true)
    try {
      const result = await runWorkflow(workflowId)
      setRunResult(result)
      toast.success("工作流运行完成")
    } catch {
      toast.error("运行失败")
    } finally {
      setRunning(false)
    }
  }

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="size-8 animate-spin text-primary" />
      </div>
    )
  }

  if (!workflow) {
    return (
      <div className="flex h-64 items-center justify-center text-muted-foreground">
        工作流不存在
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{workflow.name}</h1>
          <p className="text-muted-foreground">{workflow.description || "暂无描述"}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" asChild>
            <Link href={`/app/workflows/${workflow.id}/edit`}>编辑</Link>
          </Button>
          <Button onClick={handleRun} disabled={running}>
            {running && <Loader2 className="mr-2 size-4 animate-spin" />}
            <Play className="mr-2 size-4" />
            运行
          </Button>
        </div>
      </div>

      <div className="flex items-center gap-4 text-sm text-muted-foreground">
        <Badge variant="secondary">{workflow.status}</Badge>
        <span className="flex items-center gap-1">
          <GitBranch className="size-4" />
          {workflow.nodes.length} 节点 · {workflow.edges.length} 连接
        </span>
      </div>

      <WorkflowViewer workflow={workflow} readOnly />

      {runResult !== null && (
        <Card>
          <CardHeader>
            <CardTitle>运行结果</CardTitle>
            <CardDescription>最近一次工作流执行输出</CardDescription>
          </CardHeader>
          <CardContent>
            <pre className="max-h-64 overflow-auto rounded-lg bg-muted p-4 text-xs">
              {JSON.stringify(runResult, null, 2)}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

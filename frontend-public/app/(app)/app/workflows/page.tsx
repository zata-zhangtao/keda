"use client"

import Link from "next/link"
import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { deleteWorkflow, listWorkflows } from "@/lib/api/workflows"
import type { Workflow } from "@/lib/types/workflow"
import { GitBranch, Loader2, Pencil, Play, Plus, Trash2 } from "lucide-react"
import { toast } from "sonner"

/** Render the workflows page. */
export default function WorkflowsPage() {
  const [workflows, setWorkflows] = useState<Workflow[]>([])
  const [loading, setLoading] = useState(true)

  const loadWorkflows = () => {
    listWorkflows()
      .then(setWorkflows)
      .catch(() => toast.error("加载工作流失败"))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    const fetchWorkflows = async () => {
      try {
        const data = await listWorkflows()
        setWorkflows(data)
      } catch {
        toast.error("加载工作流失败")
      } finally {
        setLoading(false)
      }
    }
    fetchWorkflows()
  }, [])

  const handleDelete = async (workflowId: string) => {
    try {
      await deleteWorkflow(workflowId)
      toast.success("已删除")
      loadWorkflows()
    } catch {
      toast.error("删除失败")
    }
  }

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="size-8 animate-spin text-primary" />
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">工作流</h1>
          <p className="text-muted-foreground">管理和运行你的工作流</p>
        </div>
        <Button asChild>
          <Link href="/app/workflows/new">
            <Plus className="mr-1 size-4" />
            新建工作流
          </Link>
        </Button>
      </div>

      {workflows.length === 0 ? (
        <Card className="py-16 text-center">
          <CardContent>
            <GitBranch className="mx-auto size-12 text-muted-foreground" />
            <h2 className="mt-4 text-xl font-semibold">还没有工作流</h2>
            <p className="mt-2 text-muted-foreground">创建你的第一个可视化工作流</p>
            <Button className="mt-6" asChild>
              <Link href="/app/workflows/new">新建工作流</Link>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {workflows.map((workflow) => (
            <Card key={workflow.id}>
              <CardHeader>
                <div className="flex items-start justify-between">
                  <div>
                    <CardTitle className="text-lg">{workflow.name}</CardTitle>
                    <CardDescription className="line-clamp-1">
                      {workflow.description || "暂无描述"}
                    </CardDescription>
                  </div>
                  <Badge variant="secondary">{workflow.status}</Badge>
                </div>
              </CardHeader>
              <CardContent>
                <p className="mb-4 text-sm text-muted-foreground">
                  {workflow.nodes.length} 个节点 · {workflow.edges.length} 条连接
                </p>
                <div className="flex gap-2">
                  <Button size="sm" variant="outline" asChild>
                    <Link href={`/app/workflows/${workflow.id}`}>
                      <Play className="mr-1 size-4" />
                      运行
                    </Link>
                  </Button>
                  <Button size="sm" variant="outline" asChild>
                    <Link href={`/app/workflows/${workflow.id}/edit`}>
                      <Pencil className="mr-1 size-4" />
                      编辑
                    </Link>
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="text-destructive hover:bg-destructive/10"
                    onClick={() => handleDelete(workflow.id)}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}

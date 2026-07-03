"use client"

import { useRouter } from "next/navigation"
import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { WorkflowCanvas } from "@/components/workflow/workflow-canvas"
import { createWorkflow } from "@/lib/api/workflows"
import { Loader2, Save } from "lucide-react"
import { toast } from "sonner"
import type { Edge, Node } from "@xyflow/react"
import type { CustomNodeData } from "@/components/workflow/workflow-node"

const noop = () => {}
const EMPTY_NODES: Node<CustomNodeData>[] = []
const EMPTY_EDGES: Edge[] = []

/** Render the newworkflow page. */
export default function NewWorkflowPage() {
  const router = useRouter()
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [submitting, setSubmitting] = useState(false)

  const handleSave = async () => {
    setSubmitting(true)
    try {
      const workflow = await createWorkflow({ name, description })
      router.push(`/app/workflows/${workflow.id}/edit`)
    } catch {
      toast.error("创建工作流失败")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">新建工作流</h1>
          <p className="text-muted-foreground">拖拽节点编排自动化流程</p>
        </div>
        <Button onClick={handleSave} disabled={submitting || !name.trim()}>
          {submitting && <Loader2 className="mr-2 size-4 animate-spin" />}
          <Save className="mr-2 size-4" />
          保存
        </Button>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="name">名称</Label>
          <Input
            id="name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="例如：代码审查流程"
            required
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="description">描述</Label>
          <Textarea
            id="description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="描述这个工作流的用途"
            rows={1}
          />
        </div>
      </div>

      <WorkflowCanvas
        initialNodes={EMPTY_NODES}
        initialEdges={EMPTY_EDGES}
        onChange={noop}
      />
    </div>
  )
}

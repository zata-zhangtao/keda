"use client"

import { useCallback, useEffect, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { WorkflowCanvas } from "@/components/workflow/workflow-canvas"
import { getWorkflow, updateWorkflow } from "@/lib/api/workflows"
import type { Workflow } from "@/lib/types/workflow"
import { Loader2, Save } from "lucide-react"
import { toast } from "sonner"
import type { Edge, Node } from "@xyflow/react"
import type { CustomNodeData } from "@/components/workflow/workflow-node"

/** Render the editworkflow page. */
export default function EditWorkflowPage() {
  const params = useParams()
  const router = useRouter()
  const workflowId = params.id as string
  const [workflow, setWorkflow] = useState<Workflow | null>(null)
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [nodes, setNodes] = useState<Node<CustomNodeData>[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    getWorkflow(workflowId)
      .then((data) => {
        setWorkflow(data)
        setName(data.name)
        setDescription(data.description)
        setNodes(
          data.nodes.map((node) => ({
            id: node.id || `node-${Date.now()}-${Math.random()}`,
            type: "custom",
            position: { x: node.position_x || 0, y: node.position_y || 0 },
            data: {
              nodeType: node.node_type,
              label: node.label,
              config: node.config || {},
            },
          }))
        )
        setEdges(
          data.edges.map((edge) => ({
            id: edge.id || `edge-${Date.now()}-${Math.random()}`,
            source: edge.source_node_id,
            target: edge.target_node_id,
          }))
        )
      })
      .catch(() => toast.error("加载工作流失败"))
      .finally(() => setLoading(false))
  }, [workflowId])

  const handleChange = useCallback(
    (newNodes: Node<CustomNodeData>[], newEdges: Edge[]) => {
      setNodes(newNodes)
      setEdges(newEdges)
    },
    []
  )

  const handleSave = async () => {
    setSubmitting(true)
    try {
      await updateWorkflow(workflowId, {
        name,
        description,
        nodes: nodes.map((node) => ({
          id: node.id,
          node_type: node.data.nodeType,
          label: node.data.label,
          config: node.data.config || {},
          position_x: node.position.x,
          position_y: node.position.y,
        })),
        edges: edges.map((edge) => ({
          id: edge.id,
          source_node_id: edge.source,
          target_node_id: edge.target,
        })),
        status: workflow?.status,
      })
      toast.success("工作流已保存")
      router.push(`/app/workflows/${workflowId}`)
    } catch {
      toast.error("保存失败")
    } finally {
      setSubmitting(false)
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
          <h1 className="text-3xl font-bold">编辑工作流</h1>
          <p className="text-muted-foreground">拖拽节点并保存</p>
        </div>
        <Button onClick={handleSave} disabled={submitting}>
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
            required
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="description">描述</Label>
          <Textarea
            id="description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            rows={1}
          />
        </div>
      </div>

      <WorkflowCanvas
        initialNodes={nodes}
        initialEdges={edges}
        onChange={handleChange}
      />
    </div>
  )
}

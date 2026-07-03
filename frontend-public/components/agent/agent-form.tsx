"use client"

import { useEffect, useState } from "react"
import { useRouter } from "next/navigation"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { Checkbox } from "@/components/ui/checkbox"
import { createAgent, updateAgent } from "@/lib/api/agents"
import { listTools } from "@/lib/api/tools"
import type { Agent, AgentStatus } from "@/lib/types/agent"
import type { ToolDefinition } from "@/lib/types/tool"
import { Loader2 } from "lucide-react"
import { toast } from "sonner"

const AVAILABLE_MODELS = [
  { value: "openai/gpt-4o-mini", label: "OpenAI GPT-4o Mini" },
  { value: "openai/gpt-4o", label: "OpenAI GPT-4o" },
  { value: "anthropic/claude-3-5-sonnet", label: "Anthropic Claude 3.5 Sonnet" },
]

interface AgentFormProps {
  initialAgent?: Agent
}

/** Form for creating or editing an agent. */
export function AgentForm({ initialAgent }: AgentFormProps) {
  const router = useRouter()
  const [name, setName] = useState(initialAgent?.name || "")
  const [description, setDescription] = useState(initialAgent?.description || "")
  const [systemPrompt, setSystemPrompt] = useState(initialAgent?.system_prompt || "")
  const [model, setModel] = useState(initialAgent?.model || AVAILABLE_MODELS[0].value)
  const [toolIds, setToolIds] = useState<string[]>(initialAgent?.tool_ids || [])
  const [tools, setTools] = useState<ToolDefinition[]>([])
  const [status, setStatus] = useState<AgentStatus>(initialAgent?.status || "idle")
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    listTools().then(setTools).catch(() => toast.error("加载工具失败"))
  }, [])

  const toggleTool = (toolId: string) => {
    setToolIds((prev) =>
      prev.includes(toolId) ? prev.filter((id) => id !== toolId) : [...prev, toolId]
    )
  }

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    setSubmitting(true)

    try {
      const payload = {
        name,
        description,
        system_prompt: systemPrompt,
        model,
        tool_ids: toolIds,
        status,
      }

      if (initialAgent) {
        await updateAgent(initialAgent.id, payload)
        toast.success("Agent 已更新")
        router.push(`/app/agents/${initialAgent.id}`)
      } else {
        const created = await createAgent(payload)
        toast.success("Agent 已创建")
        router.push(`/app/agents/${created.id}`)
      }
    } catch {
      toast.error(initialAgent ? "更新失败" : "创建失败")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex max-w-2xl flex-col gap-6">
      <div className="space-y-2">
        <Label htmlFor="name">名称</Label>
        <Input
          id="name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="例如：代码审查助手"
          required
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="description">描述</Label>
        <Input
          id="description"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="简单描述这个 Agent 的用途"
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="systemPrompt">系统提示词</Label>
        <Textarea
          id="systemPrompt"
          value={systemPrompt}
          onChange={(event) => setSystemPrompt(event.target.value)}
          placeholder="You are a helpful coding assistant..."
          rows={6}
          required
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="model">模型</Label>
        <select
          id="model"
          value={model}
          onChange={(event) => setModel(event.target.value)}
          className="w-full rounded-md border bg-background px-3 py-2 text-sm"
        >
          {AVAILABLE_MODELS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </div>

      {initialAgent && (
        <div className="space-y-2">
          <Label htmlFor="status">状态</Label>
          <select
            id="status"
            value={status}
            onChange={(event) => setStatus(event.target.value as AgentStatus)}
            className="w-full rounded-md border bg-background px-3 py-2 text-sm"
          >
            <option value="idle">空闲</option>
            <option value="running">运行中</option>
            <option value="error">错误</option>
            <option value="offline">离线</option>
          </select>
        </div>
      )}

      <div className="space-y-2">
        <Label>启用的工具</Label>
        <div className="space-y-2">
          {tools.map((tool) => (
            <div key={tool.id} className="flex items-start gap-2">
              <Checkbox
                id={`tool-${tool.id}`}
                checked={toolIds.includes(tool.id)}
                onCheckedChange={() => toggleTool(tool.id)}
              />
              <div className="grid gap-1 leading-none">
                <Label htmlFor={`tool-${tool.id}`} className="text-sm font-medium">
                  {tool.name}
                </Label>
                <p className="text-xs text-muted-foreground">{tool.description}</p>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="flex gap-4">
        <Button type="submit" disabled={submitting}>
          {submitting && <Loader2 className="mr-2 size-4 animate-spin" />}
          {initialAgent ? "保存" : "创建"}
        </Button>
        <Button type="button" variant="outline" onClick={() => router.back()}>
          取消
        </Button>
      </div>
    </form>
  )
}

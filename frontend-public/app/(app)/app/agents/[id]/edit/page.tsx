"use client"

import { useEffect, useState } from "react"
import { useParams } from "next/navigation"
import { Loader2 } from "lucide-react"
import { AgentForm } from "@/components/agent/agent-form"
import { getAgent } from "@/lib/api/agents"
import type { Agent } from "@/lib/types/agent"
import { toast } from "sonner"

/** Render the editagent page. */
export default function EditAgentPage() {
  const params = useParams()
  const agentId = params.id as string
  const [agent, setAgent] = useState<Agent | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    getAgent(agentId)
      .then(setAgent)
      .catch(() => toast.error("加载 Agent 失败"))
      .finally(() => setLoading(false))
  }, [agentId])

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="size-8 animate-spin text-primary" />
      </div>
    )
  }

  if (!agent) {
    return (
      <div className="flex h-64 items-center justify-center text-muted-foreground">
        Agent 不存在
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-3xl font-bold">编辑 Agent</h1>
        <p className="text-muted-foreground">更新 {agent.name} 的配置</p>
      </div>
      <AgentForm initialAgent={agent} />
    </div>
  )
}

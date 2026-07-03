"use client"

import Link from "next/link"
import { useEffect, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { getAgent } from "@/lib/api/agents"
import { createSession } from "@/lib/api/sessions"
import type { Agent } from "@/lib/types/agent"
import { Bot, Loader2, MessageSquare, Pencil } from "lucide-react"
import { toast } from "sonner"

/** Render the agentdetail page. */
export default function AgentDetailPage() {
  const params = useParams()
  const router = useRouter()
  const agentId = params.id as string
  const [agent, setAgent] = useState<Agent | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    getAgent(agentId)
      .then(setAgent)
      .catch(() => toast.error("加载 Agent 失败"))
      .finally(() => setLoading(false))
  }, [agentId])

  const handleChat = async () => {
    try {
      const session = await createSession({ agent_id: agentId })
      router.push(`/app/chat/${session.id}`)
    } catch {
      toast.error("创建会话失败")
    }
  }

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
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{agent.name}</h1>
          <p className="text-muted-foreground">{agent.description || "暂无描述"}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" asChild>
            <Link href={`/app/agents/${agent.id}/edit`}>
              <Pencil className="mr-1 size-4" />
              编辑
            </Link>
          </Button>
          <Button onClick={handleChat}>
            <MessageSquare className="mr-1 size-4" />
            开始会话
          </Button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Bot className="size-5 text-primary" />
              配置信息
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <div className="text-sm font-medium text-muted-foreground">模型</div>
              <div>{agent.model}</div>
            </div>
            <div>
              <div className="text-sm font-medium text-muted-foreground">状态</div>
              <Badge variant={agent.status === "running" ? "default" : "secondary"}>
                {agent.status}
              </Badge>
            </div>
            <div>
              <div className="text-sm font-medium text-muted-foreground">系统提示词</div>
              <div className="mt-1 rounded-lg bg-muted p-4 text-sm whitespace-pre-wrap">
                {agent.system_prompt}
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>启用的工具</CardTitle>
            <CardDescription>该 Agent 可以调用的工具</CardDescription>
          </CardHeader>
          <CardContent>
            {agent.tool_ids.length === 0 ? (
              <p className="text-sm text-muted-foreground">未启用任何工具</p>
            ) : (
              <ul className="space-y-2">
                {agent.tool_ids.map((toolId) => (
                  <li
                    key={toolId}
                    className="rounded-lg border px-3 py-2 text-sm font-medium"
                  >
                    {toolId}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

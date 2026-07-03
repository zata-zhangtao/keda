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
import { deleteAgent, listAgents } from "@/lib/api/agents"
import type { Agent } from "@/lib/types/agent"
import { Bot, Loader2, MessageSquare, Pencil, Plus, Trash2 } from "lucide-react"
import { toast } from "sonner"

/** Render the agents page. */
export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [loading, setLoading] = useState(true)

  const loadAgents = () => {
    listAgents()
      .then(setAgents)
      .catch(() => toast.error("加载 Agent 失败"))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    const fetchAgents = async () => {
      try {
        const data = await listAgents()
        setAgents(data)
      } catch {
        toast.error("加载 Agent 失败")
      } finally {
        setLoading(false)
      }
    }
    fetchAgents()
  }, [])

  const handleDelete = async (agentId: string) => {
    try {
      await deleteAgent(agentId)
      toast.success("已删除")
      loadAgents()
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
          <h1 className="text-3xl font-bold">我的 Agent</h1>
          <p className="text-muted-foreground">管理你的 Agent 配置</p>
        </div>
        <Button asChild>
          <Link href="/app/agents/new">
            <Plus className="mr-1 size-4" />
            新建 Agent
          </Link>
        </Button>
      </div>

      {agents.length === 0 ? (
        <Card className="py-16 text-center">
          <CardContent>
            <Bot className="mx-auto size-12 text-muted-foreground" />
            <h2 className="mt-4 text-xl font-semibold">还没有 Agent</h2>
            <p className="mt-2 text-muted-foreground">创建你的第一个 Agent 开始自动化工作</p>
            <Button className="mt-6" asChild>
              <Link href="/app/agents/new">新建 Agent</Link>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {agents.map((agent) => (
            <Card key={agent.id}>
              <CardHeader>
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-3">
                    <div className="flex size-10 items-center justify-center rounded-lg bg-primary/10">
                      <Bot className="size-5 text-primary" />
                    </div>
                    <div>
                      <CardTitle className="text-lg">{agent.name}</CardTitle>
                      <CardDescription className="line-clamp-1">
                        {agent.model}
                      </CardDescription>
                    </div>
                  </div>
                  <span
                    className={`size-2 rounded-full ${
                      agent.status === "running"
                        ? "bg-emerald-500"
                        : agent.status === "error"
                          ? "bg-rose-500"
                          : "bg-slate-400"
                    }`}
                    title={agent.status}
                  />
                </div>
              </CardHeader>
              <CardContent>
                <p className="mb-4 line-clamp-2 text-sm text-muted-foreground">
                  {agent.description || "暂无描述"}
                </p>
                <div className="flex gap-2">
                  <Button size="sm" variant="outline" asChild>
                    <Link href={`/app/agents/${agent.id}`}>
                      <MessageSquare className="mr-1 size-4" />
                      聊天
                    </Link>
                  </Button>
                  <Button size="sm" variant="outline" asChild>
                    <Link href={`/app/agents/${agent.id}/edit`}>
                      <Pencil className="mr-1 size-4" />
                      编辑
                    </Link>
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="text-destructive hover:bg-destructive/10"
                    onClick={() => handleDelete(agent.id)}
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

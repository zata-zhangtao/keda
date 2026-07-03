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
import { listAgents } from "@/lib/api/agents"
import type { Agent } from "@/lib/types/agent"
import { Bot, Loader2, MessageSquare } from "lucide-react"

/** Render the agentsmarketplace page. */
export default function AgentsMarketplacePage() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    listAgents()
      .then(setAgents)
      .catch(() => setAgents([]))
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="container mx-auto py-16">
      <div className="mx-auto max-w-5xl">
        <div className="mb-10 text-center">
          <h1 className="text-4xl font-bold">Agent 广场</h1>
          <p className="mt-3 text-muted-foreground">
            浏览社区和平台预置的 Agent，也可以登录后创建自己的 Agent
          </p>
        </div>

        {loading ? (
          <div className="flex h-64 items-center justify-center">
            <Loader2 className="size-8 animate-spin text-primary" />
          </div>
        ) : agents.length === 0 ? (
          <div className="rounded-2xl border bg-muted/30 px-8 py-16 text-center">
            <Bot className="mx-auto size-12 text-muted-foreground" />
            <h2 className="mt-4 text-xl font-semibold">暂无公开 Agent</h2>
            <p className="mt-2 text-muted-foreground">
              登录后创建你的第一个 Agent，它将会出现在这里。
            </p>
            <Button className="mt-6" asChild>
              <Link href="/register">免费注册</Link>
            </Button>
          </div>
        ) : (
          <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
            {agents.map((agent) => (
              <Card key={agent.id} className="bg-card/50">
                <CardHeader>
                  <div className="flex items-center gap-3">
                    <div className="flex size-10 items-center justify-center rounded-lg bg-primary/10">
                      <Bot className="size-5 text-primary" />
                    </div>
                    <div>
                      <CardTitle className="text-lg">{agent.name}</CardTitle>
                      <CardDescription className="line-clamp-1">
                        {agent.description || "暂无描述"}
                      </CardDescription>
                    </div>
                  </div>
                </CardHeader>
                <CardContent>
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">
                      模型: {agent.model}
                    </span>
                    <Button size="sm" variant="outline" asChild>
                      <Link href={`/register?next=/app/agents/${agent.id}`}>
                        <MessageSquare className="mr-1 size-4" />
                        试用
                      </Link>
                    </Button>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

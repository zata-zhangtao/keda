"use client"

import { useEffect, useState } from "react"
import { Loader2 } from "lucide-react"
import { SessionList } from "@/components/chat/session-list"
import { listSessions } from "@/lib/api/sessions"
import type { ChatSession } from "@/lib/types/session"
import { toast } from "sonner"

/** Render the chatcenter page. */
export default function ChatCenterPage() {
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    listSessions()
      .then(setSessions)
      .catch(() => toast.error("加载会话失败"))
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
    <div className="flex h-[calc(100vh-4rem)] gap-0 rounded-2xl border">
      <div className="w-64">
        <SessionList sessions={sessions} />
      </div>
      <div className="flex flex-1 items-center justify-center text-muted-foreground">
        选择一个会话开始聊天，或从 Agent 详情页发起新会话
      </div>
    </div>
  )
}

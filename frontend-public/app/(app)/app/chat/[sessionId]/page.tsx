"use client"

import { useEffect, useRef, useState } from "react"
import { useParams } from "next/navigation"
import { Loader2 } from "lucide-react"
import { ChatInput } from "@/components/chat/chat-input"
import { ChatMessage } from "@/components/chat/chat-message"
import { SessionList } from "@/components/chat/session-list"
import { getSession, listMessages, listSessions, sendMessage } from "@/lib/api/sessions"
import type { ChatMessage as ChatMessageType, ChatSession } from "@/lib/types/session"
import { toast } from "sonner"

/** Render the chatsession page. */
export default function ChatSessionPage() {
  const params = useParams()
  const sessionId = params.sessionId as string
  const [session, setSession] = useState<ChatSession | null>(null)
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [messages, setMessages] = useState<ChatMessageType[]>([])
  const [loading, setLoading] = useState(true)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const loadData = async () => {
      try {
        const [sessionData, sessionsData, messagesData] = await Promise.all([
          getSession(sessionId),
          listSessions(),
          listMessages(sessionId),
        ])
        setSession(sessionData)
        setSessions(sessionsData)
        setMessages(messagesData)
      } catch {
        toast.error("加载会话失败")
      } finally {
        setLoading(false)
      }
    }
    loadData()
  }, [sessionId])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages])

  const handleSend = async (content: string) => {
    const tempId = `temp-${Date.now()}`
    setMessages((prev) => [
      ...prev,
      {
        id: tempId,
        session_id: sessionId,
        role: "user",
        content,
        tool_calls: [],
      },
    ])

    try {
      await sendMessage(sessionId, { content })
      const updatedMessages = await listMessages(sessionId)
      setMessages(updatedMessages)
    } catch {
      toast.error("发送失败")
      setMessages((prev) => prev.filter((message) => message.id !== tempId))
    }
  }

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="size-8 animate-spin text-primary" />
      </div>
    )
  }

  if (!session) {
    return (
      <div className="flex h-64 items-center justify-center text-muted-foreground">
        会话不存在
      </div>
    )
  }

  return (
    <div className="flex h-[calc(100vh-4rem)] gap-0 rounded-2xl border">
      <div className="w-64">
        <SessionList sessions={sessions} activeSessionId={sessionId} />
      </div>
      <div className="flex flex-1 flex-col bg-background">
        <div className="border-b px-6 py-4">
          <h1 className="font-semibold">{session.title}</h1>
          <p className="text-xs text-muted-foreground">Agent: {session.agent_id}</p>
        </div>
        <div className="flex-1 space-y-6 overflow-auto px-6 py-6">
          {messages.map((message) => (
            <ChatMessage key={message.id} message={message} />
          ))}
          <div ref={messagesEndRef} />
        </div>
        <div className="px-6 pb-6">
          <ChatInput onSend={handleSend} />
        </div>
      </div>
    </div>
  )
}

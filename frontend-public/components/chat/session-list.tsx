"use client"

import Link from "next/link"
import { cn } from "@/lib/utils"
import type { ChatSession } from "@/lib/types/session"
import { MessageSquare } from "lucide-react"

interface SessionListProps {
  sessions: ChatSession[]
  activeSessionId?: string
}

/** List of chat sessions with active selection. */
export function SessionList({ sessions, activeSessionId }: SessionListProps) {
  return (
    <div className="flex h-full flex-col border-r bg-sidebar">
      <div className="flex h-14 items-center border-b px-4">
        <h2 className="font-semibold text-sidebar-foreground">会话</h2>
      </div>
      <div className="flex-1 overflow-auto p-2">
        {sessions.length === 0 ? (
          <p className="px-3 py-4 text-sm text-sidebar-foreground/60">暂无会话</p>
        ) : (
          <ul className="space-y-1">
            {sessions.map((session) => (
              <li key={session.id}>
                <Link
                  href={`/app/chat/${session.id}`}
                  className={cn(
                    "flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                    activeSessionId === session.id
                      ? "bg-sidebar-primary text-sidebar-primary-foreground"
                      : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
                  )}
                >
                  <MessageSquare className="size-4" />
                  <span className="truncate">{session.title}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

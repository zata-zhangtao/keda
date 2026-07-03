import { cn } from "@/lib/utils"
import type { ChatMessage as ChatMessageType } from "@/lib/types/session"
import { Bot, User } from "lucide-react"
import { ToolCallCard } from "./tool-call-card"

interface ChatMessageProps {
  message: ChatMessageType
}

/** Render a single chat message bubble. */
export function ChatMessage({ message }: ChatMessageProps) {
  const isUser = message.role === "user"

  return (
    <div
      className={cn(
        "flex gap-3",
        isUser ? "flex-row-reverse" : "flex-row"
      )}
    >
      <div
        className={cn(
          "flex size-8 shrink-0 items-center justify-center rounded-full",
          isUser ? "bg-primary text-primary-foreground" : "bg-muted"
        )}
      >
        {isUser ? <User className="size-4" /> : <Bot className="size-4" />}
      </div>
      <div
        className={cn(
          "max-w-[80%] rounded-2xl px-4 py-3",
          isUser
            ? "bg-primary text-primary-foreground"
            : "border bg-card"
        )}
      >
        <div className="whitespace-pre-wrap text-sm">{message.content}</div>
        {message.tool_calls && message.tool_calls.length > 0 && (
          <div className="mt-3 space-y-2">
            {message.tool_calls.map((toolCall) => (
              <ToolCallCard key={toolCall.id} toolCall={toolCall} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

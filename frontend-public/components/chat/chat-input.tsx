"use client"

import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Loader2, Send } from "lucide-react"

interface ChatInputProps {
  onSend: (content: string) => void | Promise<void>
  disabled?: boolean
}

/** Chat message input with send handling. */
export function ChatInput({ onSend, disabled }: ChatInputProps) {
  const [content, setContent] = useState("")
  const [sending, setSending] = useState(false)

  const handleSubmit = async (event?: React.FormEvent) => {
    event?.preventDefault()
    if (!content.trim() || sending || disabled) return

    setSending(true)
    try {
      await onSend(content.trim())
      setContent("")
    } finally {
      setSending(false)
    }
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      handleSubmit()
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex items-end gap-2 border-t pt-4">
      <Textarea
        value={content}
        onChange={(event) => setContent(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="输入消息...（Shift + Enter 换行）"
        rows={2}
        disabled={disabled || sending}
        className="min-h-[60px] flex-1 resize-none"
      />
      <Button type="submit" size="icon" disabled={disabled || sending}>
        {sending ? (
          <Loader2 className="size-4 animate-spin" />
        ) : (
          <Send className="size-4" />
        )}
      </Button>
    </form>
  )
}

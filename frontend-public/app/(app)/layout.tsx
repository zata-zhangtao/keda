"use client"

import { useRouter } from "next/navigation"
import { useEffect } from "react"
import { AppShell } from "@/components/layout/app-shell"
import { SessionProvider, useSession } from "@/components/auth/SessionProvider"

/** 受保护子树的路由守卫：未登录或会话恢复出错时跳回 /login。 */
function RequireSession({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const { status } = useSession()

  useEffect(() => {
    if (status === "anonymous" || status === "error") {
      const redirectPath = window.location.pathname + window.location.search
      router.replace(`/login?redirect=${encodeURIComponent(redirectPath)}`)
    }
  }, [status, router])

  if (status === "loading") {
    return (
      <div className="flex min-h-svh items-center justify-center text-muted-foreground">
        加载中…
      </div>
    )
  }

  if (status !== "authenticated") {
    return null
  }

  return <AppShell>{children}</AppShell>
}

/** (app) 区根布局：用 SessionProvider 包裹受保护子树，行为对齐 keda/frontend。 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <SessionProvider>
      <RequireSession>{children}</RequireSession>
    </SessionProvider>
  )
}

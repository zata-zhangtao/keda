"use client"

import { useRouter } from "next/navigation"
import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { logout, getCurrentSession, type UserSession } from "@/lib/api/auth"

/** Render the settings page. */
export default function SettingsPage() {
  const router = useRouter()
  const [user, setUser] = useState<UserSession | null>(null)

  useEffect(() => {
    getCurrentSession().then(setUser).catch(() => router.replace("/login"))
  }, [router])

  /** Handle logout and redirect to the home page. */
  async function handleLogout() {
    await logout()
    router.replace("/")
  }

  if (!user) {
    return (
      <div className="flex h-64 items-center justify-center text-muted-foreground">
        加载中…
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-3xl font-bold">设置</h1>
        <p className="text-muted-foreground">
          {user.display_name} · {user.email}
        </p>
      </div>
      <div className="rounded-2xl border bg-muted/30 p-6">
        <h2 className="mb-2 text-lg font-semibold">关于 Zata Agent Platform</h2>
        <p className="text-sm text-muted-foreground">
          当前版本为 MVP 演示，真实 LLM 调用需要在后端配置 provider 和 API key。
          工作流执行目前返回 mock 结果，用于验证编排流程。
        </p>
      </div>
      <Button variant="destructive" onClick={handleLogout}>
        退出登录
      </Button>
    </div>
  )
}

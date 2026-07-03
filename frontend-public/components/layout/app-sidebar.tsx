"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import { cn } from "@/lib/utils"
import {
  Activity,
  BarChart3,
  GitBranch,
  LayoutDashboard,
  Lightbulb,
  Map,
  Settings,
} from "lucide-react"

// 导航项对齐 keda/frontend 吸收进来的 agent-runner 监控页面；
// settings 保留以承载模板原有的登出入口。
const navItems = [
  { href: "/app/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/app/processes", label: "Processes", icon: Activity },
  { href: "/app/repositories", label: "Repositories", icon: GitBranch },
  { href: "/app/stats", label: "Stats", icon: BarChart3 },
  { href: "/app/roadmap", label: "Roadmap", icon: Map },
  { href: "/app/ideas", label: "Ideas", icon: Lightbulb },
  { href: "/app/settings", label: "Settings", icon: Settings },
]

/** Sidebar navigation for authenticated pages. */
export function AppSidebar() {
  const pathname = usePathname()

  return (
    <aside className="flex w-64 flex-col border-r bg-sidebar">
      <div className="flex h-14 items-center gap-2 border-b px-4">
        <span className="size-6 rounded-md bg-primary" />
        <span className="font-semibold text-sidebar-foreground">Zata</span>
      </div>
      <nav className="flex-1 p-3">
        <ul className="space-y-1">
          {navItems.map((item) => (
            <li key={item.href}>
              <Link
                href={item.href}
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                  pathname === item.href || pathname?.startsWith(`${item.href}/`)
                    ? "bg-sidebar-primary text-sidebar-primary-foreground"
                    : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
                )}
              >
                <item.icon className="size-4" />
                {item.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </aside>
  )
}

import { AppSidebar } from "./app-sidebar"

interface AppShellProps {
  children: React.ReactNode
}

/** Main application shell wrapping sidebar and content. */
export function AppShell({ children }: AppShellProps) {
  return (
    <div className="flex min-h-svh">
      <AppSidebar />
      <main className="flex-1 overflow-auto">
        <div className="container mx-auto p-8">{children}</div>
      </main>
    </div>
  )
}

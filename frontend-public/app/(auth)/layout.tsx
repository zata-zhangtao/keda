/** Root layout for the app section. */
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-svh flex-col items-center justify-center bg-muted/50 p-6">
      <div className="w-full max-w-sm">{children}</div>
    </div>
  )
}

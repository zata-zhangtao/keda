import Link from "next/link"

/** Site footer for public pages. */
export function SiteFooter() {
  return (
    <footer className="border-t bg-muted/50">
      <div className="container mx-auto flex flex-col gap-4 py-8 md:flex-row md:items-center md:justify-between">
        <div className="flex items-center gap-2 font-semibold">
          <span className="size-5 rounded-md bg-primary" />
          Zata Agent Platform
        </div>
        <p className="text-sm text-muted-foreground">
          © {new Date().getFullYear()} Zata. All rights reserved.
        </p>
        <nav className="flex gap-4 text-sm text-muted-foreground">
          <Link href="/marketplace" className="hover:text-foreground">
            Agent 广场
          </Link>
          <Link href="/features" className="hover:text-foreground">
            功能
          </Link>
          <Link href="/pricing" className="hover:text-foreground">
            定价
          </Link>
          <Link href="/about" className="hover:text-foreground">
            关于
          </Link>
        </nav>
      </div>
    </footer>
  )
}

import Link from "next/link"
import { Button } from "@/components/ui/button"

/** Site header for public pages. */
export function SiteHeader() {
  return (
    <header className="sticky top-0 z-50 w-full border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="container mx-auto flex h-14 items-center justify-between">
        <div className="flex items-center gap-6">
          <Link href="/" className="flex items-center gap-2 font-semibold">
            <span className="size-6 rounded-md bg-primary" />
            Zata Agent Platform
          </Link>
          <nav className="hidden gap-4 text-sm text-muted-foreground md:flex">
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
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" asChild>
            <Link href="/login">登录</Link>
          </Button>
          <Button size="sm" asChild>
            <Link href="/register">免费试用</Link>
          </Button>
        </div>
      </div>
    </header>
  )
}

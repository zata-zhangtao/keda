import { SiteHeader } from "./site-header"
import { SiteFooter } from "./site-footer"

/** Marketing/public page layout with header and footer. */
export function PublicLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader />
      <main className="flex-1">{children}</main>
      <SiteFooter />
    </div>
  )
}

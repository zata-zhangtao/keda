import { PublicLayout } from "@/components/layout/public-layout"

/** Root layout for the app section. */
export default function Layout({ children }: { children: React.ReactNode }) {
  return <PublicLayout>{children}</PublicLayout>
}

import { createFileRoute, redirect } from '@tanstack/react-router'
import { getCurrentSession } from '@/api/auth'
import { useAuthStore } from '@/stores/auth-store'
import { AuthenticatedLayout } from '@/components/layout/authenticated-layout'

export const Route = createFileRoute('/_authenticated')({
  beforeLoad: async ({ location }) => {
    try {
      const session = await getCurrentSession()
      useAuthStore.getState().auth.setUser(session)
    } catch {
      useAuthStore.getState().auth.reset()
      throw redirect({
        to: '/sign-in',
        search: { redirect: location.pathname + location.searchStr },
      })
    }
  },
  component: AuthenticatedLayout,
})

import { useNavigate, useLocation } from '@tanstack/react-router'
import { toast } from 'sonner'
import { logout } from '@/api/auth'
import { useAuthStore } from '@/stores/auth-store'
import { ConfirmDialog } from '@/components/confirm-dialog'

interface SignOutDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** Render the SignOutDialog component. */
export function SignOutDialog({ open, onOpenChange }: SignOutDialogProps) {
  const navigate = useNavigate()
  const location = useLocation()
  const { auth } = useAuthStore()

  const handleSignOut = async () => {
    try {
      await logout()
    } catch {
      // ignore logout errors, still clear local state
    }
    auth.reset()
    const currentPath = location.href
    navigate({
      to: '/sign-in',
      search: { redirect: currentPath },
      replace: true,
    })
    toast.success('已退出登录')
  }

  return (
    <ConfirmDialog
      open={!!open}
      onOpenChange={onOpenChange}
      title='退出登录'
      desc='确定要退出登录吗？退出后需要重新登录才能访问。'
      confirmText='退出'
      destructive
      handleConfirm={handleSignOut}
      className='sm:max-w-sm'
    />
  )
}

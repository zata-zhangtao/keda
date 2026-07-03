import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  type PublicUser,
  disablePublicUser,
  enablePublicUser,
  listPublicUsers,
} from '@/api/users'
import { ConfigDrawer } from '@/components/config-drawer'
import { Header } from '@/components/layout/header'
import { Main } from '@/components/layout/main'
import { ProfileDropdown } from '@/components/profile-dropdown'
import { Search } from '@/components/search'
import { ThemeSwitch } from '@/components/theme-switch'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

const PUBLIC_USERS_QUERY_KEY = ['admin', 'public-users'] as const

/** Format an ISO date string for display. */
function formatDateTime(value: string | null): string {
  if (!value) return '-'
  const parsedDate = new Date(value)
  return Number.isNaN(parsedDate.getTime()) ? '-' : parsedDate.toLocaleString()
}

/** Render the Users component. */
export function Users() {
  const queryClient = useQueryClient()
  const [keywordInput, setKeywordInput] = useState('')
  const [appliedKeyword, setAppliedKeyword] = useState('')
  const [pendingUser, setPendingUser] = useState<PublicUser | null>(null)

  const usersQuery = useQuery({
    queryKey: [...PUBLIC_USERS_QUERY_KEY, appliedKeyword],
    queryFn: () =>
      listPublicUsers(appliedKeyword ? { keyword: appliedKeyword } : {}),
  })

  const toggleMutation = useMutation({
    mutationFn: (targetUser: PublicUser) =>
      targetUser.status === 'active'
        ? disablePublicUser(targetUser.id)
        : enablePublicUser(targetUser.id),
    onSuccess: (updatedUser) => {
      toast.success(
        updatedUser.status === 'active'
          ? `已启用 ${updatedUser.display_name}`
          : `已禁用 ${updatedUser.display_name}`
      )
      void queryClient.invalidateQueries({ queryKey: PUBLIC_USERS_QUERY_KEY })
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : '操作失败')
    },
    onSettled: () => setPendingUser(null),
  })

  const publicUsers: PublicUser[] = usersQuery.data?.items ?? []

  /** Render the table body rows. */
  function renderTableBody() {
    if (usersQuery.isLoading) {
      return (
        <TableRow>
          <TableCell colSpan={5} className='h-24 text-center'>
            加载中…
          </TableCell>
        </TableRow>
      )
    }
    if (usersQuery.isError) {
      return (
        <TableRow>
          <TableCell colSpan={5} className='h-24 text-center text-destructive'>
            加载用户失败
          </TableCell>
        </TableRow>
      )
    }
    if (publicUsers.length === 0) {
      return (
        <TableRow>
          <TableCell colSpan={5} className='h-24 text-center'>
            暂无用户
          </TableCell>
        </TableRow>
      )
    }
    return publicUsers.map((publicUser) => (
      <TableRow key={publicUser.id}>
        <TableCell>{publicUser.display_name}</TableCell>
        <TableCell>{publicUser.email}</TableCell>
        <TableCell>
          <Badge
            variant='outline'
            className={
              publicUser.status === 'active'
                ? 'border-teal-200 bg-teal-100/30 text-teal-900 dark:text-teal-200'
                : 'border-destructive/20 bg-destructive/10 text-destructive'
            }
          >
            {publicUser.status === 'active' ? '正常' : '已禁用'}
          </Badge>
        </TableCell>
        <TableCell>{formatDateTime(publicUser.created_at)}</TableCell>
        <TableCell className='text-end'>
          <Button
            variant={publicUser.status === 'active' ? 'outline' : 'default'}
            size='sm'
            onClick={() => setPendingUser(publicUser)}
          >
            {publicUser.status === 'active' ? '禁用' : '启用'}
          </Button>
        </TableCell>
      </TableRow>
    ))
  }

  return (
    <>
      <Header fixed>
        <Search className='me-auto' />
        <ThemeSwitch />
        <ConfigDrawer />
        <ProfileDropdown />
      </Header>

      <Main className='flex flex-1 flex-col gap-4 sm:gap-6'>
        <div className='flex flex-wrap items-end justify-between gap-2'>
          <div>
            <h2 className='text-2xl font-bold tracking-tight'>用户</h2>
            <p className='text-muted-foreground'>管理 C 端注册用户的状态。</p>
          </div>
        </div>

        <form
          className='flex items-center gap-2'
          onSubmit={(event) => {
            event.preventDefault()
            setAppliedKeyword(keywordInput.trim())
          }}
        >
          <Input
            placeholder='按邮箱或显示名搜索…'
            value={keywordInput}
            onChange={(event) => setKeywordInput(event.target.value)}
            className='h-9 w-64'
          />
          <Button type='submit' variant='outline' size='sm'>
            搜索
          </Button>
        </form>

        <div className='overflow-hidden rounded-md border'>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>显示名</TableHead>
                <TableHead>邮箱</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>注册时间</TableHead>
                <TableHead className='text-end'>操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>{renderTableBody()}</TableBody>
          </Table>
        </div>
      </Main>

      <AlertDialog
        open={pendingUser !== null}
        onOpenChange={(open) => {
          if (!open) setPendingUser(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {pendingUser?.status === 'active' ? '禁用用户' : '启用用户'}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pendingUser?.status === 'active'
                ? `禁用后，${pendingUser?.display_name} 将无法登录，且其当前会话立即失效。`
                : `启用后，${pendingUser?.display_name} 可以重新登录。`}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={toggleMutation.isPending}>
              取消
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={toggleMutation.isPending}
              onClick={() => {
                if (pendingUser) toggleMutation.mutate(pendingUser)
              }}
            >
              确认
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

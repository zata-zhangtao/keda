import { useSearch } from '@tanstack/react-router'
import { Card, CardContent } from '@/components/ui/card'
import { AuthLayout } from '../auth-layout'
import { UserAuthForm } from './components/user-auth-form'

/** Render the SignIn component. */
export function SignIn() {
  const { redirect } = useSearch({ from: '/(auth)/sign-in' })

  return (
    <AuthLayout>
      <div className='flex flex-col gap-6'>
        <div className='flex flex-col gap-2 text-center'>
          <h1 className='text-2xl font-semibold tracking-tight'>欢迎回来</h1>
          <p className='text-sm text-muted-foreground'>
            请输入账号信息登录 Zata 管理系统
          </p>
        </div>

        <Card className='border shadow-lg'>
          <CardContent className='pt-6'>
            <UserAuthForm redirectTo={redirect} />
          </CardContent>
        </Card>

        <p className='text-center text-xs text-muted-foreground'>
          登录即表示您同意我们的服务条款与隐私政策
        </p>
      </div>
    </AuthLayout>
  )
}

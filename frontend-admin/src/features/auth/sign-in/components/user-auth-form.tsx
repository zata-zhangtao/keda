import { useState } from 'react'
import { z } from 'zod'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { useNavigate } from '@tanstack/react-router'
import { Loader2, LogIn } from 'lucide-react'
import { toast } from 'sonner'
import { login } from '@/api/auth'
import { useAuthStore } from '@/stores/auth-store'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { PasswordInput } from '@/components/password-input'

const formSchema = z.object({
  identifier: z.string().min(1, '请输入用户名或邮箱'),
  password: z.string().min(1, '请输入密码'),
  rememberMe: z.boolean().optional(),
})

interface UserAuthFormProps extends React.HTMLAttributes<HTMLFormElement> {
  redirectTo?: string
}

/** Render the UserAuthForm component. */
export function UserAuthForm({
  className,
  redirectTo,
  ...props
}: UserAuthFormProps) {
  const [isLoading, setIsLoading] = useState(false)
  const navigate = useNavigate()
  const { auth } = useAuthStore()

  const form = useForm<z.infer<typeof formSchema>>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      identifier: '',
      password: '',
      rememberMe: false,
    },
  })

  /** Submit the sign-in form. */
  async function onSubmit(data: z.infer<typeof formSchema>) {
    setIsLoading(true)
    try {
      const session = await login({
        identifier: data.identifier,
        password: data.password,
      })
      auth.setUser(session)
      if (redirectTo) {
        navigate({ href: redirectTo, replace: true })
      } else {
        navigate({ to: '/', replace: true })
      }
      toast.success(`欢迎回来，${session.display_name}`)
    } catch (error) {
      const message = error instanceof Error ? error.message : '登录失败'
      toast.error(message)
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit(onSubmit)}
        className={cn('grid gap-5', className)}
        {...props}
      >
        <FormField
          control={form.control}
          name='identifier'
          render={({ field }) => (
            <FormItem>
              <FormLabel>用户名 / 邮箱</FormLabel>
              <FormControl>
                <Input data-testid='admin-login-identifier-input' placeholder='admin@example.com' {...field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name='password'
          render={({ field }) => (
            <FormItem className='relative'>
              <div className='flex items-center justify-between'>
                <FormLabel>密码</FormLabel>
                <button
                  type='button'
                  className='text-xs text-muted-foreground underline-offset-4 hover:text-primary hover:underline'
                  onClick={() =>
                    toast.info('请联系管理员重置密码')
                  }
                >
                  忘记密码？
                </button>
              </div>
              <FormControl>
                <PasswordInput data-testid='admin-login-password-input' placeholder='********' {...field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name='rememberMe'
          render={({ field }) => (
            <FormItem className='flex flex-row items-center space-y-0 gap-2'>
              <FormControl>
                <Checkbox
                  checked={field.value}
                  onCheckedChange={field.onChange}
                />
              </FormControl>
              <FormLabel className='text-xs font-normal text-muted-foreground'>
                记住我
              </FormLabel>
            </FormItem>
          )}
        />
        <Button data-testid='admin-login-submit-button' size='lg' disabled={isLoading}>
          {isLoading ? (
            <Loader2 className='mr-2 size-4 animate-spin' />
          ) : (
            <LogIn className='mr-2 size-4' />
          )}
          登录
        </Button>
      </form>
    </Form>
  )
}

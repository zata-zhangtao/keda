"use client"

import Link from "next/link"
import { useRouter, useSearchParams } from "next/navigation"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"
import { Loader2, LogIn } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { PasswordInput } from "@/components/ui/password-input"
import { login } from "@/lib/api/auth"

const formSchema = z.object({
  identifier: z.string().min(1, "请输入用户名或邮箱"),
  password: z.string().min(1, "请输入密码"),
})

/** Render the LoginForm component. */
export function LoginForm() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const [isLoading, setIsLoading] = useState(false)

  const form = useForm<z.infer<typeof formSchema>>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      identifier: "",
      password: "",
    },
  })

  /** Submit the login form. */
  async function onSubmit(data: z.infer<typeof formSchema>) {
    setIsLoading(true)
    try {
      await login(data)
      toast.success("登录成功")
      const redirect = searchParams.get("redirect")
      router.replace(redirect || "/app/dashboard")
    } catch (error) {
      const message = error instanceof Error ? error.message : "登录失败"
      toast.error(message)
    } finally {
      setIsLoading(false)
    }
  }

  const handleFormSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    void form.handleSubmit(onSubmit)(event)
  }

  return (
    <Form {...form}>
      <form onSubmit={handleFormSubmit} className="grid gap-4">
        <FormField
          control={form.control}
          name="identifier"
          render={({ field }) => (
            <FormItem>
              <FormLabel>用户名 / 邮箱</FormLabel>
              <FormControl>
                <Input
                  data-testid="login-identifier-input"
                  placeholder="admin@example.com"
                  {...field}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="password"
          render={({ field }) => (
            <FormItem>
              <div className="flex items-center justify-between">
                <FormLabel>密码</FormLabel>
                <button
                  type="button"
                  className="text-xs text-muted-foreground underline-offset-4 hover:text-primary hover:underline"
                  onClick={() => toast.info("请联系管理员重置密码")}
                >
                  忘记密码？
                </button>
              </div>
              <FormControl>
                <PasswordInput
                  data-testid="login-password-input"
                  placeholder="********"
                  {...field}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <Button data-testid="login-submit-button" type="submit" disabled={isLoading}>
          {isLoading ? (
            <Loader2 className="mr-2 size-4 animate-spin" />
          ) : (
            <LogIn className="mr-2 size-4" />
          )}
          登录
        </Button>
      </form>
      <p className="mt-6 text-center text-sm text-muted-foreground">
        还没有账号？{" "}
        <Link
          href="/register"
          className="text-primary underline-offset-4 hover:underline"
        >
          立即注册
        </Link>
      </p>
    </Form>
  )
}

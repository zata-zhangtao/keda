import { Suspense } from "react"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { LoginForm } from "./login-form"

/** Render the login page. */
export default function LoginPage() {
  return (
    <Card className="border shadow-lg">
      <CardHeader className="text-center">
        <CardTitle className="text-2xl">欢迎回来</CardTitle>
        <CardDescription>请输入账号信息登录 Zata</CardDescription>
      </CardHeader>
      <CardContent>
        <Suspense fallback={<div className="py-8 text-center text-sm text-muted-foreground">加载中…</div>}>
          <LoginForm />
        </Suspense>
      </CardContent>
    </Card>
  )
}

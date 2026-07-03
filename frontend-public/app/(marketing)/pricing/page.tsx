import Link from "next/link"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Check } from "lucide-react"

const plans = [
  {
    name: "免费版",
    price: "¥0",
    description: "适合个人开发者试用。",
    features: ["最多 3 个 Agent", "基础工具调用", "公开 Agent 广场", "社区支持"],
    cta: "免费开始",
    href: "/register",
    variant: "outline" as const,
  },
  {
    name: "专业版",
    price: "¥99",
    period: "/月",
    description: "适合成长型开发者团队。",
    features: [
      "无限 Agent",
      "可视化工作流编排",
      "高级模型配置",
      "邮件支持",
    ],
    cta: "立即升级",
    href: "/register",
    variant: "default" as const,
  },
  {
    name: "企业版",
    price: "定制",
    description: "适合大型组织。",
    features: [
      "无限 Agent 与工作流",
      "SSO 单点登录",
      "私有化部署选项",
      "专属客户成功经理",
    ],
    cta: "联系我们",
    href: "/about",
    variant: "outline" as const,
  },
]

/** Render the pricing page. */
export default function PricingPage() {
  return (
    <div className="container mx-auto py-16">
      <div className="mx-auto max-w-3xl text-center">
        <h1 className="text-3xl font-bold md:text-5xl">定价方案</h1>
        <p className="mt-4 text-muted-foreground">
          选择适合你的方案，随时升级或降级。
        </p>
      </div>
      <div className="mx-auto mt-12 grid max-w-5xl gap-6 md:grid-cols-3">
        {plans.map((plan) => (
          <Card key={plan.name} className="flex flex-col">
            <CardHeader>
              <CardTitle>{plan.name}</CardTitle>
              <CardDescription>{plan.description}</CardDescription>
              <div className="mt-4 flex items-baseline">
                <span className="text-3xl font-bold">{plan.price}</span>
                {plan.period && (
                  <span className="text-muted-foreground">{plan.period}</span>
                )}
              </div>
            </CardHeader>
            <CardContent className="flex-1">
              <ul className="space-y-3">
                {plan.features.map((feature) => (
                  <li key={feature} className="flex items-center gap-2 text-sm">
                    <Check className="size-4 text-primary" />
                    {feature}
                  </li>
                ))}
              </ul>
            </CardContent>
            <CardFooter>
              <Button className="w-full" variant={plan.variant} asChild>
                <Link href={plan.href}>{plan.cta}</Link>
              </Button>
            </CardFooter>
          </Card>
        ))}
      </div>
    </div>
  )
}

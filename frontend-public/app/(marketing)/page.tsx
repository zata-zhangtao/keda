import Link from "next/link"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  ArrowRight,
  Bot,
  CheckCircle2,
  Cpu,
  GitBranch,
  Layers,
  MessageSquare,
  Plug,
  Sparkles,
} from "lucide-react"

const features = [
  {
    icon: Bot,
    title: "Agent 广场",
    description: "浏览、创建和配置专属 Agent，为不同开发场景定制系统提示词与能力边界。",
  },
  {
    icon: Plug,
    title: "工具调用",
    description: "为 Agent 挂载搜索、代码执行等工具，让 Agent 真正动手解决问题。",
  },
  {
    icon: GitBranch,
    title: "工作流编排",
    description: "通过可视化画布拖拽节点，把多个 Agent 和工具编排成可复用的自动化流程。",
  },
  {
    icon: MessageSquare,
    title: "多轮会话",
    description: "与 Agent 持续对话，观察工具调用过程，随时调整方向。",
  },
  {
    icon: Cpu,
    title: "模型自选",
    description: "按需选择底层大模型，灵活平衡能力、成本与响应速度。",
  },
  {
    icon: Layers,
    title: "团队共享",
    description: "在团队内共享 Agent 配置与工作流，沉淀最佳实践。",
  },
]

const stats = [
  { value: "10+", label: "预置工具" },
  { value: "∞", label: "可编排节点" },
  { value: "99.9%", label: "服务可用性" },
]

const faqs = [
  {
    question: "Zata Agent Platform 适合什么场景？",
    answer:
      "适合需要自动化代码审查、技术问答、API 文档生成、故障排查、CI/CD 辅助等开发者工作流。",
  },
  {
    question: "注册后是否需要绑定信用卡？",
    answer: "免费版无需绑定信用卡即可开始创建 Agent 和会话。",
  },
  {
    question: "可以连接自己的大模型 API 吗？",
    answer:
      "可以，平台通过 provider/model_id 形式配置模型端点，支持所有 OpenAI 兼容协议的服务。",
  },
]

/** Render the home page. */
export default function HomePage() {
  return (
    <div className="flex flex-col gap-20 pb-20">
      {/* Hero */}
      <section className="container mx-auto pt-16 md:pt-24">
        <div className="mx-auto grid max-w-6xl items-center gap-12 lg:grid-cols-2">
          <div className="flex flex-col gap-6">
            <div className="inline-flex w-fit items-center gap-2 rounded-full border bg-muted/50 px-3 py-1 text-xs font-medium">
              <Sparkles className="size-4 text-primary" />
              面向开发者团队的 AI Agent 平台
            </div>
            <h1
              data-testid="public-hero-heading"
              className="text-4xl font-bold tracking-tight md:text-6xl"
            >
              让 Agent
              <span className="text-primary">为你工作</span>
            </h1>
            <p className="text-lg text-muted-foreground md:text-xl">
              创建专属 Agent、挂载工具、编排工作流。把重复的开发者任务交给 AI，
              让团队专注于创造更高价值的代码。
            </p>
            <div className="flex flex-wrap gap-4">
              <Button size="lg" asChild>
                <Link href="/register">
                  免费开始
                  <ArrowRight className="ml-2 size-4" />
                </Link>
              </Button>
              <Button size="lg" variant="outline" asChild>
                <Link href="/marketplace">浏览 Agent 广场</Link>
              </Button>
            </div>
            <div className="flex items-center gap-4 text-sm text-muted-foreground">
              <div className="flex items-center gap-1">
                <CheckCircle2 className="size-4 text-primary" />
                免费试用
              </div>
              <div className="flex items-center gap-1">
                <CheckCircle2 className="size-4 text-primary" />
                无需信用卡
              </div>
              <div className="flex items-center gap-1">
                <CheckCircle2 className="size-4 text-primary" />
                即时开通
              </div>
            </div>
          </div>
          <div className="relative hidden lg:block">
            <div className="absolute inset-0 rounded-3xl bg-gradient-to-br from-primary/30 via-accent/20 to-muted opacity-60 blur-3xl" />
            <div className="relative rounded-2xl border bg-card/80 p-6 shadow-xl backdrop-blur">
              <div className="mb-4 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <div className="size-3 rounded-full bg-red-500" />
                  <div className="size-3 rounded-full bg-yellow-500" />
                  <div className="size-3 rounded-full bg-green-500" />
                </div>
                <span className="text-xs text-muted-foreground">
                  Zata Agent Workbench
                </span>
              </div>
              <div className="space-y-3">
                <div className="flex items-center gap-3 rounded-lg bg-muted p-4">
                  <Bot className="size-6 text-primary" />
                  <div className="flex-1">
                    <div className="h-2 w-24 rounded bg-primary/20" />
                    <div className="mt-2 h-2 w-full rounded bg-muted-foreground/20" />
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div className="h-24 rounded-lg bg-muted" />
                  <div className="h-24 rounded-lg bg-muted" />
                </div>
                <div className="h-32 rounded-lg bg-muted" />
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Stats */}
      <section className="container mx-auto">
        <div className="mx-auto max-w-5xl">
          <div className="grid gap-6 rounded-2xl border bg-muted/30 px-8 py-12 text-center md:grid-cols-3">
            {stats.map((stat) => (
              <div key={stat.label}>
                <div className="text-4xl font-bold text-primary">{stat.value}</div>
                <div className="mt-2 text-sm text-muted-foreground">{stat.label}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Features */}
      <section className="container mx-auto">
        <div className="mx-auto max-w-5xl">
          <div className="mb-10 text-center">
            <h2 className="text-3xl font-bold">核心能力</h2>
            <p className="mt-3 text-muted-foreground">
              从单一 Agent 到多节点工作流，覆盖开发者日常自动化需求
            </p>
          </div>
          <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
            {features.map((item) => (
              <Card key={item.title} className="bg-card/50">
                <CardHeader>
                  <item.icon className="size-8 text-primary" />
                  <CardTitle className="mt-2">{item.title}</CardTitle>
                  <CardDescription>{item.description}</CardDescription>
                </CardHeader>
              </Card>
            ))}
          </div>
        </div>
      </section>

      {/* How it works */}
      <section className="container mx-auto">
        <div className="mx-auto max-w-5xl">
          <div className="mb-10 text-center">
            <h2 className="text-3xl font-bold">三步上手</h2>
            <p className="mt-3 text-muted-foreground">
              几分钟内让你的第一个 Agent 跑起来
            </p>
          </div>
          <div className="grid gap-6 md:grid-cols-3">
            {[
              {
                step: "01",
                title: "创建 Agent",
                description: "选择模型、编写系统提示词、挂载需要的工具。",
              },
              {
                step: "02",
                title: "开始会话",
                description: "与 Agent 对话，观察它如何调用工具完成任务。",
              },
              {
                step: "03",
                title: "编排工作流",
                description: "把 Agent 和工具组合成可视化工作流，实现自动化。",
              },
            ].map((item) => (
              <Card key={item.step} className="relative overflow-hidden">
                <CardHeader>
                  <span className="text-4xl font-bold text-muted-foreground/30">
                    {item.step}
                  </span>
                  <CardTitle className="mt-2">{item.title}</CardTitle>
                  <CardDescription>{item.description}</CardDescription>
                </CardHeader>
              </Card>
            ))}
          </div>
        </div>
      </section>

      {/* FAQ */}
      <section className="container mx-auto">
        <div className="mx-auto max-w-3xl">
          <div className="mb-10 text-center">
            <h2 className="text-3xl font-bold">常见问题</h2>
            <p className="mt-3 text-muted-foreground">
              关于 Zata Agent Platform 你可能想知道的事
            </p>
          </div>
          <div className="space-y-4">
            {faqs.map((faq) => (
              <Card key={faq.question}>
                <CardHeader>
                  <CardTitle className="text-base">{faq.question}</CardTitle>
                  <CardDescription className="text-sm leading-relaxed">
                    {faq.answer}
                  </CardDescription>
                </CardHeader>
              </Card>
            ))}
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="container mx-auto">
        <div className="mx-auto max-w-4xl rounded-2xl bg-primary px-6 py-16 text-center text-primary-foreground md:px-12">
          <h2 className="text-3xl font-bold">准备好开始了吗？</h2>
          <p className="mx-auto mt-4 max-w-xl text-primary-foreground/80">
            注册账号，立即创建你的第一个 Agent，体验 AI 驱动的开发者工作流。
          </p>
          <Button
            size="lg"
            variant="secondary"
            className="mt-8"
            asChild
          >
            <Link href="/register">免费注册</Link>
          </Button>
        </div>
      </section>
    </div>
  )
}

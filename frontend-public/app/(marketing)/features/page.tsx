import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Bot, GitBranch, MessageSquare, Plug, Shield, Zap } from "lucide-react"

const features = [
  {
    icon: Bot,
    title: "Agent 市场",
    description: "创建、管理和分享面向开发者的 Agent。",
  },
  {
    icon: Plug,
    title: "工具调用",
    description: "为 Agent 挂载搜索、代码执行等工具，扩展能力边界。",
  },
  {
    icon: GitBranch,
    title: "可视化工作流",
    description: "拖拽节点、连接边，把 Agent 编排成可复用的自动化流程。",
  },
  {
    icon: MessageSquare,
    title: "多轮会话",
    description: "与 Agent 持续对话，实时观察工具调用与执行结果。",
  },
  {
    icon: Zap,
    title: "快速响应",
    description: "基于现代技术栈构建，低延迟、高并发。",
  },
  {
    icon: Shield,
    title: "安全认证",
    description: "HttpOnly Session + 密码存储，保护用户数据。",
  },
]

/** Render the features page. */
export default function FeaturesPage() {
  return (
    <div className="container mx-auto py-16">
      <div className="mx-auto max-w-5xl">
        <div className="mb-10 text-center">
          <h1 className="text-4xl font-bold">功能特性</h1>
          <p className="mt-3 text-muted-foreground">
            Zata Agent Platform 为开发者团队提供完整的 Agent 构建与编排能力
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
    </div>
  )
}

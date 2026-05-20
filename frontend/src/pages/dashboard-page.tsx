import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

// TODO: 替换为实际业务内容
const placeholderCards = [
  { title: "总用户数", value: "—", description: "连接后端后显示" },
  { title: "今日请求", value: "—", description: "连接后端后显示" },
  { title: "活跃会话", value: "—", description: "连接后端后显示" },
];

export function DashboardPage() {
  return (
    <div className="flex flex-col gap-6 p-6">
      <div>
        <h2 className="text-xl font-semibold text-slate-900">概览</h2>
        <p className="text-sm text-slate-500 mt-1">欢迎使用，这是 Dashboard 占位页面。</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {placeholderCards.map((card) => (
          <Card key={card.title}>
            <CardHeader>
              <CardTitle className="text-sm font-medium text-slate-500">{card.title}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-slate-900">{card.value}</div>
              <CardDescription className="mt-1">{card.description}</CardDescription>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import type { RoadmapPrd } from "@/lib/api/types";

const STATE_LABELS: Record<RoadmapPrd["state"], string> = {
  not_started: "未开始",
  ready: "就绪",
  running: "运行中",
  supervising: "监督中",
  review: "待审阅",
  failed: "失败",
  blocked: "阻塞",
  merged: "已合并",
  archived: "已归档",
  unresolved_dependency: "依赖未解析",
  waiting: "等待中",
};

const STATE_VARIANTS: Record<RoadmapPrd["state"], BadgeVariant> = {
  not_started: "default",
  ready: "ready",
  running: "running",
  supervising: "supervising",
  review: "review",
  failed: "failed",
  blocked: "blocked",
  merged: "warning",
  archived: "default",
  unresolved_dependency: "error",
  waiting: "warning",
};

interface PrdCardProps {
  prd: RoadmapPrd;
  onStart: () => void;
  starting: boolean;
}

export function PrdCard({ prd, onStart, starting }: PrdCardProps) {
  const progress =
    prd.acceptance_total > 0
      ? Math.round((prd.acceptance_checked / prd.acceptance_total) * 100)
      : 0;

  const isStartable =
    prd.state === "not_started" || prd.state === "failed" || prd.state === "waiting";

  const highlightClass =
    prd.state === "review"
      ? "border-amber-400 dark:border-amber-600"
      : prd.state === "merged"
        ? "border-emerald-400 dark:border-emerald-600"
        : undefined;

  return (
    <Card className={highlightClass}>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <CardTitle className="truncate text-base" title={prd.title}>
              {prd.title}
            </CardTitle>
            <p className="mt-0.5 truncate text-xs text-slate-500" title={prd.prd_path}>
              {prd.prd_path}
            </p>
          </div>
          <Badge variant={STATE_VARIANTS[prd.state]} className="shrink-0">
            {STATE_LABELS[prd.state]}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center gap-3 text-sm">
          <span className="text-slate-500">进度</span>
          <Progress value={progress} className="h-2 flex-1" />
          <span className="text-xs text-slate-500">{progress}%</span>
        </div>

        {prd.issue_number ? (
          <p className="text-xs text-slate-500">
            Issue #{prd.issue_number}
            {prd.issue_url ? (
              <a
                href={prd.issue_url}
                target="_blank"
                rel="noreferrer"
                className="ml-1 text-blue-600 hover:underline"
              >
                查看
              </a>
            ) : null}
          </p>
        ) : null}

        {prd.block_reason ? (
          <p className="text-xs text-red-600">{prd.block_reason}</p>
        ) : null}

        {prd.delivery_dependencies.length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {prd.delivery_dependencies.map((dep) => (
              <span
                key={`${dep.from_path}-${dep.to_path}`}
                className="inline-flex items-center rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600 dark:bg-slate-800 dark:text-slate-400"
                title={dep.detail}
              >
                {dep.kind === "prd" ? "→" : dep.kind === "issue" ? "#" : "G:"} {dep.to_path}
              </span>
            ))}
          </div>
        ) : null}

        <div className="flex items-center gap-2 pt-1">
          {isStartable ? (
            <Button size="sm" onClick={onStart} disabled={starting || !!prd.block_reason}>
              {starting ? "启动中…" : "开始"}
            </Button>
          ) : null}
          {prd.next_action?.url ? (
            <Button size="sm" variant="outline" asChild>
              <a href={prd.next_action.url} target="_blank" rel="noreferrer">
                {prd.next_action.label}
              </a>
            </Button>
          ) : prd.next_action ? (
            <span className="text-xs text-slate-500">{prd.next_action.label}</span>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

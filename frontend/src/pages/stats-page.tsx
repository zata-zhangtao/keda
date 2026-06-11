// 统计页：实时完成度（GitHub 口径）+ 本地运行历史趋势（SQLite 口径）。

import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatLocalDateTime } from "@/lib/utils";
import {
  fetchCompletionStats,
  fetchRecentRuns,
  fetchRunHistoryTrend,
} from "@shared/api/console";
import type {
  DailyRunTrendEntry,
  RepositoryCompletionStats,
  RunRecordEntry,
} from "@shared/api/types";

export function StatsPage() {
  const [stats, setStats] = useState<RepositoryCompletionStats[] | null>(null);
  const [trendRepoId, setTrendRepoId] = useState("");
  const [trendDays, setTrendDays] = useState(30);
  const [trend, setTrend] = useState<DailyRunTrendEntry[]>([]);
  const [recentRuns, setRecentRuns] = useState<RunRecordEntry[]>([]);

  useEffect(() => {
    fetchCompletionStats()
      .then(setStats)
      .catch((error: unknown) => {
        toast.error(
          error instanceof Error ? error.message : "无法加载完成度统计。",
        );
        setStats([]);
      });
  }, []);

  useEffect(() => {
    fetchRunHistoryTrend({ repoId: trendRepoId || undefined, days: trendDays })
      .then(setTrend)
      .catch((error: unknown) => {
        toast.error(
          error instanceof Error ? error.message : "无法加载历史趋势。",
        );
      });
    fetchRecentRuns({ repoId: trendRepoId || undefined, limit: 30 })
      .then(setRecentRuns)
      .catch(() => setRecentRuns([]));
  }, [trendRepoId, trendDays]);

  const trendMax = useMemo(
    () =>
      Math.max(
        1,
        ...trend.map((entry) => entry.completed + entry.failed + entry.blocked),
      ),
    [trend],
  );

  return (
    <div className="flex flex-col gap-4 p-4 lg:p-6">
      <div>
        <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
          完成度统计
        </h2>
        <p className="mt-1 text-sm text-slate-500">
          实时口径来自 GitHub（closed Issue 含 agent workflow label 计为已处理）；
          历史趋势来自本地运行记录（CLI 直跑与面板托管共用）。
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">实时完成度（GitHub）</CardTitle>
        </CardHeader>
        <CardContent>
          {stats === null ? (
            <Skeleton className="h-24" />
          ) : stats.length === 0 ? (
            <p className="text-sm text-slate-500">暂无仓库统计。</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700">
                    <th className="py-2 pr-3">仓库</th>
                    <th className="py-2 pr-3">完成率</th>
                    <th className="py-2 pr-3">完成</th>
                    <th className="py-2 pr-3">失败</th>
                    <th className="py-2 pr-3">Blocked</th>
                    <th className="py-2 pr-3">进行中</th>
                    <th className="py-2 pr-3">总计</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.map((entry) => (
                    <tr
                      key={entry.repo_id}
                      className="border-b border-slate-100 dark:border-slate-800"
                    >
                      <td className="py-2 pr-3 font-medium">
                        {entry.display_name}
                        {entry.truncated ? (
                          <Badge variant="warning" className="ml-2 text-[10px]">
                            截断
                          </Badge>
                        ) : null}
                        {entry.error ? (
                          <Badge variant="warning" className="ml-2 text-[10px]">
                            查询失败
                          </Badge>
                        ) : null}
                      </td>
                      <td className="py-2 pr-3">
                        <CompletionRateBar rate={entry.completion_rate} />
                      </td>
                      <td className="py-2 pr-3 text-emerald-700 dark:text-emerald-400">
                        {entry.completed}
                      </td>
                      <td className="py-2 pr-3 text-red-700 dark:text-red-400">
                        {entry.failed}
                      </td>
                      <td className="py-2 pr-3 text-amber-700 dark:text-amber-400">
                        {entry.blocked}
                      </td>
                      <td className="py-2 pr-3">{entry.open_in_pipeline}</td>
                      <td className="py-2 pr-3">{entry.total_tracked}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="text-sm">历史趋势（本地运行记录）</CardTitle>
            <div className="flex items-center gap-2">
              <select
                className="h-8 rounded-md border border-slate-200 bg-transparent px-2 text-xs dark:border-slate-700"
                value={trendRepoId}
                onChange={(event) => setTrendRepoId(event.target.value)}
                aria-label="趋势仓库筛选"
              >
                <option value="">全部仓库</option>
                {(stats ?? []).map((entry) => (
                  <option key={entry.repo_id} value={entry.repo_id}>
                    {entry.display_name}
                  </option>
                ))}
              </select>
              <select
                className="h-8 rounded-md border border-slate-200 bg-transparent px-2 text-xs dark:border-slate-700"
                value={trendDays}
                onChange={(event) => setTrendDays(Number(event.target.value))}
                aria-label="趋势天数"
              >
                <option value={7}>7 天</option>
                <option value={30}>30 天</option>
                <option value={90}>90 天</option>
              </select>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {trend.length === 0 ? (
            <p className="text-sm text-slate-500">
              所选范围内暂无运行记录。运行 <code>iar run</code> 或通过面板触发
              执行后，这里会出现按天聚合的成功/失败曲线。
            </p>
          ) : (
            <div className="flex items-end gap-1 overflow-x-auto pb-2">
              {trend.map((entry) => {
                const total = entry.completed + entry.failed + entry.blocked;
                return (
                  <div
                    key={entry.day}
                    className="flex min-w-7 flex-col items-center gap-1"
                    title={`${entry.day}：完成 ${entry.completed} / 失败 ${entry.failed} / blocked ${entry.blocked}`}
                  >
                    <div className="flex h-32 w-5 flex-col-reverse overflow-hidden rounded-sm bg-slate-100 dark:bg-slate-800">
                      <div
                        className="w-full bg-emerald-500"
                        style={{ height: `${(entry.completed / trendMax) * 100}%` }}
                      />
                      <div
                        className="w-full bg-red-500"
                        style={{ height: `${(entry.failed / trendMax) * 100}%` }}
                      />
                      <div
                        className="w-full bg-amber-500"
                        style={{ height: `${(entry.blocked / trendMax) * 100}%` }}
                      />
                    </div>
                    <span className="text-[10px] text-slate-500">
                      {entry.day.slice(5)}
                    </span>
                    <span className="text-[10px] font-medium">{total}</span>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">最近运行记录</CardTitle>
        </CardHeader>
        <CardContent>
          {recentRuns.length === 0 ? (
            <p className="text-sm text-slate-500">暂无运行记录。</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700">
                    <th className="py-2 pr-3">时间</th>
                    <th className="py-2 pr-3">仓库</th>
                    <th className="py-2 pr-3">Issue</th>
                    <th className="py-2 pr-3">结果</th>
                    <th className="py-2 pr-3">触发</th>
                    <th className="py-2 pr-3">Agent</th>
                    <th className="py-2 pr-3">耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {recentRuns.map((run, index) => (
                    <tr
                      key={`${run.repo_id}-${run.issue_number}-${run.started_at}-${index}`}
                      className="border-b border-slate-100 dark:border-slate-800"
                    >
                      <td className="py-2 pr-3 font-mono text-[11px] text-slate-500">
                        {formatLocalDateTime(run.started_at)}
                      </td>
                      <td className="py-2 pr-3">{run.repo_id}</td>
                      <td className="py-2 pr-3 font-mono text-xs">
                        #{run.issue_number}
                      </td>
                      <td className="py-2 pr-3">
                        <OutcomeBadge outcome={run.outcome} />
                      </td>
                      <td className="py-2 pr-3 text-xs">{run.trigger}</td>
                      <td className="py-2 pr-3 text-xs">{run.agent}</td>
                      <td className="py-2 pr-3 text-xs">
                        {formatDuration(run.duration_seconds)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function CompletionRateBar({ rate }: { rate: number | null }) {
  if (rate === null) {
    return <span className="text-xs text-slate-400">—</span>;
  }
  const percent = Math.round(rate * 100);
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 w-24 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
        <div
          className="h-full rounded-full bg-emerald-500"
          style={{ width: `${percent}%` }}
        />
      </div>
      <span className="text-xs font-medium">{percent}%</span>
    </div>
  );
}

function OutcomeBadge({ outcome }: { outcome: RunRecordEntry["outcome"] }) {
  const variant =
    outcome === "completed" ? "ready" : outcome === "failed" ? "warning" : "default";
  return <Badge variant={variant}>{outcome}</Badge>;
}

function formatDuration(durationSeconds: number): string {
  if (durationSeconds < 60) {
    return `${Math.round(durationSeconds)}s`;
  }
  return `${Math.round(durationSeconds / 60)}m`;
}

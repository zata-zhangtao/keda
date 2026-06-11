import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { IssueDetail } from "@/components/agent-runner/issue-detail";
import { RepositoryOverview } from "@/components/agent-runner/repository-overview";
import {
  fetchIssueDetail,
  fetchMonitoringOverview,
} from "@shared/api/agentRunner";
import {
  executeIssueAction,
  fetchCompletionStats,
} from "@shared/api/console";
import type {
  IssueMonitoringSnapshot,
  MonitoringOverview,
  RepositoryCompletionStats,
} from "@shared/api/types";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; overview: MonitoringOverview }
  | { kind: "error"; message: string };

export function DashboardPage() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [selectedIssueNumber, setSelectedIssueNumber] = useState<number | null>(
    null,
  );
  const [selectedIssue, setSelectedIssue] =
    useState<IssueMonitoringSnapshot | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [completionStats, setCompletionStats] = useState<
    RepositoryCompletionStats[]
  >([]);
  const [actionPending, setActionPending] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setState({ kind: "loading" });
    fetchMonitoringOverview()
      .then((overview) => {
        if (cancelled) {
          return;
        }
        setState({ kind: "ready", overview });
        const firstIssue = overview.repositories
          .flatMap((repo) => repo.issues)
          .find(Boolean);
        if (firstIssue) {
          setSelectedIssueNumber(firstIssue.number);
          setSelectedIssue(firstIssue);
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        setState({
          kind: "error",
          message:
            error instanceof Error
              ? error.message
              : "无法加载监控概览。",
        });
      });
    // 完成度统计独立加载，失败不影响监控主视图。
    fetchCompletionStats()
      .then((stats) => {
        if (!cancelled) {
          setCompletionStats(stats);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (selectedIssueNumber === null) {
      setSelectedIssue(null);
      return;
    }
    if (state.kind !== "ready") {
      return;
    }
    const cached = state.overview.repositories
      .flatMap((repo) => repo.issues)
      .find((issue) => issue.number === selectedIssueNumber);
    if (cached) {
      setSelectedIssue(cached);
      setDetailError(null);
      return;
    }
    let cancelled = false;
    setDetailError(null);
    fetchIssueDetail(selectedIssueNumber)
      .then((detail) => {
        if (cancelled) {
          return;
        }
        setSelectedIssue(detail);
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        setDetailError(
          error instanceof Error
            ? error.message
            : "无法加载 Issue 详情。",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [selectedIssueNumber, state]);

  const totalIssues = useMemo(() => {
    if (state.kind !== "ready") {
      return 0;
    }
    return state.overview.repositories.reduce(
      (acc, repo) => acc + repo.issues.length,
      0,
    );
  }, [state]);

  const totalAnomalies = useMemo(() => {
    if (state.kind !== "ready") {
      return 0;
    }
    return state.overview.repositories.reduce(
      (acc, repo) => acc + repo.anomaly_count,
      0,
    );
  }, [state]);

  const selectedIssueRepoId = useMemo(() => {
    if (state.kind !== "ready" || selectedIssueNumber === null) {
      return null;
    }
    for (const repository of state.overview.repositories) {
      if (repository.issues.some((issue) => issue.number === selectedIssueNumber)) {
        return repository.repo_id;
      }
    }
    return state.overview.repositories[0]?.repo_id ?? null;
  }, [state, selectedIssueNumber]);

  const statsByRepoId = useMemo(() => {
    const lookup: Record<string, RepositoryCompletionStats> = {};
    for (const entry of completionStats) {
      lookup[entry.repo_id] = entry;
    }
    return lookup;
  }, [completionStats]);

  async function handleIssueAction(
    action: "retry_failed" | "blocked_continue",
  ) {
    if (!selectedIssue || !selectedIssueRepoId) {
      return;
    }
    const verb = action === "retry_failed" ? "重试" : "继续";
    const confirmed = window.confirm(
      `确认对 Issue #${selectedIssue.number} 执行「${verb}」？`,
    );
    if (!confirmed) {
      return;
    }
    setActionPending(true);
    try {
      const result = await executeIssueAction(
        selectedIssueRepoId,
        selectedIssue.number,
        action,
      );
      toast.success(result.detail);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : `${verb}失败。`);
    } finally {
      setActionPending(false);
    }
  }

  return (
    <div className="flex flex-col gap-4 p-4 lg:p-6">
      <div>
        <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
          Agent Runner 管理终端
        </h2>
        <p className="mt-1 text-sm text-slate-500">
          多项目队列、事件时间线与异常监控。failed/blocked Issue 可直接重试或
          继续；进程托管与完成度统计见左侧导航。
        </p>
      </div>

      {state.kind === "loading" ? <LoadingSkeleton /> : null}

      {state.kind === "error" ? (
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-red-700 dark:text-red-300">
              加载监控数据失败：{state.message}
            </p>
          </CardContent>
        </Card>
      ) : null}

      {state.kind === "ready" ? (
        <>
          <SummaryStrip
            repositoryCount={state.overview.repositories.length}
            issueCount={totalIssues}
            anomalyCount={totalAnomalies}
            scannedAt={state.overview.scanned_at}
          />
          <UnreachableRepositories overview={state.overview} />
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <div className="space-y-4">
              {state.overview.repositories.map((repository) => (
                <div key={repository.repo_id} className="space-y-1">
                  <CompletionSummaryStrip
                    stats={statsByRepoId[repository.repo_id]}
                  />
                  <RepositoryOverview
                    repository={repository}
                    onSelectIssue={(issue) => setSelectedIssueNumber(issue.number)}
                    selectedIssueNumber={selectedIssueNumber}
                  />
                </div>
              ))}
            </div>
            <div className="min-h-[24rem]">
              {selectedIssue ? (
                <div className="space-y-2">
                  <IssueActionBar
                    issue={selectedIssue}
                    pending={actionPending}
                    onAction={(action) => void handleIssueAction(action)}
                  />
                  <IssueDetail issue={selectedIssue} />
                </div>
              ) : (
                <Card>
                  <CardHeader>
                    <CardTitle className="text-sm">Issue 详情</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <p className="text-sm text-slate-500">
                      点击左侧 Issue 行查看事件时间线和异常。
                    </p>
                  </CardContent>
                </Card>
              )}
              {detailError ? (
                <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
                  详情加载失败：{detailError}
                </p>
              ) : null}
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}

function IssueActionBar({
  issue,
  pending,
  onAction,
}: {
  issue: IssueMonitoringSnapshot;
  pending: boolean;
  onAction: (action: "retry_failed" | "blocked_continue") => void;
}) {
  const isFailed = issue.primary_label.includes("failed");
  const isBlocked = issue.primary_label.includes("blocked");
  if (!isFailed && !isBlocked) {
    return null;
  }
  return (
    <div className="flex items-center gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 dark:border-amber-800 dark:bg-amber-950">
      <span className="text-xs text-amber-800 dark:text-amber-200">
        {isFailed
          ? "该 Issue 处于 failed 状态，可重试（label 翻转回 ready）。"
          : "该 Issue 处于 blocked 状态，可继续（启动 blocked-continue 进程）。"}
      </span>
      <Button
        size="sm"
        variant="outline"
        className="ml-auto"
        disabled={pending}
        onClick={() => onAction(isFailed ? "retry_failed" : "blocked_continue")}
      >
        {pending ? "执行中…" : isFailed ? "重试" : "继续"}
      </Button>
    </div>
  );
}

function CompletionSummaryStrip({
  stats,
}: {
  stats: RepositoryCompletionStats | undefined;
}) {
  if (!stats || stats.total_tracked === 0) {
    return null;
  }
  const percent =
    stats.completion_rate !== null
      ? `${Math.round(stats.completion_rate * 100)}%`
      : "—";
  return (
    <div className="flex flex-wrap items-center gap-2 px-1 text-xs text-slate-500">
      <span className="font-medium text-slate-700 dark:text-slate-300">
        完成率 {percent}
      </span>
      <span>✓ {stats.completed}</span>
      <span className="text-red-600 dark:text-red-400">✗ {stats.failed}</span>
      <span className="text-amber-600 dark:text-amber-400">
        ⛔ {stats.blocked}
      </span>
      <span>进行中 {stats.open_in_pipeline}</span>
    </div>
  );
}

function UnreachableRepositories({
  overview,
}: {
  overview: MonitoringOverview;
}) {
  const unreachable = overview.unreachable_repositories ?? [];
  if (unreachable.length === 0) {
    return null;
  }
  return (
    <Card className="border-amber-300 dark:border-amber-700">
      <CardContent className="pt-4">
        <p className="mb-1 text-sm font-medium text-amber-800 dark:text-amber-200">
          {unreachable.length} 个已注册仓库无法访问（已从监控中跳过）：
        </p>
        <ul className="space-y-0.5 text-xs text-amber-700 dark:text-amber-300">
          {unreachable.map((entry) => (
            <li key={entry.repo_id}>
              <code>{entry.repo_id}</code>：{entry.configured_path} — {entry.error}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function SummaryStrip({
  repositoryCount,
  issueCount,
  anomalyCount,
  scannedAt,
}: {
  repositoryCount: number;
  issueCount: number;
  anomalyCount: number;
  scannedAt: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
      <Badge variant="default">仓库 {repositoryCount}</Badge>
      <Badge variant="default">活跃 Issue {issueCount}</Badge>
      <Badge
        variant={anomalyCount > 0 ? "warning" : "ready"}
        className="text-xs"
      >
        异常 {anomalyCount}
      </Badge>
      <span className="ml-auto font-mono text-[11px] text-slate-400">
        scanned_at {scannedAt || "—"}
      </span>
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <Skeleton className="h-64" />
      <Skeleton className="h-64" />
    </div>
  );
}

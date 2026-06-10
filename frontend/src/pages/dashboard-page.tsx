import { useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { IssueDetail } from "@/components/agent-runner/issue-detail";
import { RepositoryOverview } from "@/components/agent-runner/repository-overview";
import {
  fetchIssueDetail,
  fetchMonitoringOverview,
} from "@shared/api/agentRunner";
import type {
  IssueMonitoringSnapshot,
  MonitoringOverview,
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

  return (
    <div className="flex flex-col gap-4 p-4 lg:p-6">
      <div>
        <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
          Agent Runner Monitor
        </h2>
        <p className="mt-1 text-sm text-slate-500">
          只读面板：展示仓库队列、事件时间线与状态异常。所有恢复操作仍通过
          <code className="mx-1 rounded bg-slate-100 px-1.5 py-0.5 text-xs dark:bg-slate-800">
            iar
          </code>
          CLI 执行。
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
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <div className="space-y-4">
              {state.overview.repositories.map((repository) => (
                <RepositoryOverview
                  key={repository.repo_id}
                  repository={repository}
                  onSelectIssue={(issue) => setSelectedIssueNumber(issue.number)}
                  selectedIssueNumber={selectedIssueNumber}
                />
              ))}
            </div>
            <div className="min-h-[24rem]">
              {selectedIssue ? (
                <IssueDetail issue={selectedIssue} />
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

// Renders a single repository's monitoring overview (health, queue counts, issues).

import { useMemo } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type {
  IssueMonitoringSnapshot,
  RepositoryMonitoringOverview,
} from "@shared/api/types";

import { IssueList } from "@/components/agent-runner/issue-list";
import { variantForLabel } from "@/components/agent-runner/label-variant";

interface RepositoryOverviewProps {
  repository: RepositoryMonitoringOverview;
  onSelectIssue: (issue: IssueMonitoringSnapshot) => void;
  selectedIssueNumber: number | null;
}

const QUEUE_ORDER: Array<keyof RepositoryMonitoringOverview["queue_counts"]> = [
  "ready",
  "running",
  "supervising",
  "review",
  "failed",
  "blocked",
];

export function RepositoryOverview({
  repository,
  onSelectIssue,
  selectedIssueNumber,
}: RepositoryOverviewProps) {
  const groupedIssues = useMemo(() => {
    const groups: Record<string, IssueMonitoringSnapshot[]> = {};
    for (const queueKey of QUEUE_ORDER) {
      groups[queueKey] = [];
    }
    for (const issue of repository.issues) {
      const bucket = primaryBucket(issue.primary_label, repository.labels);
      if (bucket) {
        groups[bucket].push(issue);
      }
    }
    return groups;
  }, [repository]);

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
          <CardTitle className="flex items-center gap-2 text-base">
            <span>{repository.display_name}</span>
            <span className="text-xs font-normal text-slate-500">
              {repository.repo_id}
            </span>
          </CardTitle>
          <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <HealthPill
              label="gh"
              ok={repository.health.gh_available}
              tooltip="GitHub CLI 可用"
            />
            <HealthPill
              label="path"
              ok={repository.health.repo_path_exists}
              tooltip="仓库路径存在"
            />
            <HealthPill
              label={`remote:${repository.remote}`}
              ok={repository.health.publish_remote_exists}
              tooltip="发布 remote 已配置"
            />
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2">
          {QUEUE_ORDER.map((queueKey) => {
            const count = repository.queue_counts[queueKey];
            const label = repository.labels[queueKey];
            return (
              <Badge
                key={queueKey}
                variant={count > 0 ? variantForLabel(label) : "default"}
                className="text-xs"
              >
                {label.replace(/^agent\//, "")} {count}
              </Badge>
            );
          })}
          <Badge
            variant={
              repository.anomaly_summary.error > 0
                ? "error"
                : repository.anomaly_summary.warning > 0
                  ? "warning"
                  : "default"
            }
            className="ml-auto text-xs"
          >
            异常 {repository.anomaly_count}
            {repository.anomaly_summary.warning > 0 ||
            repository.anomaly_summary.error > 0
              ? ` (${repository.anomaly_summary.warning}w / ${repository.anomaly_summary.error}e)`
              : ""}
          </Badge>
        </div>

        {QUEUE_ORDER.map((queueKey) => {
          const issues = groupedIssues[queueKey] ?? [];
          if (issues.length === 0) {
            return null;
          }
          return (
            <div key={queueKey} className="space-y-1.5">
              <div className="flex items-center gap-2 text-xs font-medium text-slate-600">
                <span>{repository.labels[queueKey]}</span>
                <span className="text-slate-400">{issues.length}</span>
              </div>
              <IssueList
                issues={issues}
                onSelect={onSelectIssue}
                selectedIssueNumber={selectedIssueNumber}
              />
            </div>
          );
        })}

        {repository.issues.length === 0 ? (
          <p className="text-sm text-slate-500">
            当前没有匹配工作流标签的 Issue。
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function primaryBucket(
  primaryLabel: string,
  labels: RepositoryMonitoringOverview["labels"],
): keyof RepositoryMonitoringOverview["queue_counts"] | null {
  for (const key of QUEUE_ORDER) {
    if (labels[key] === primaryLabel) {
      return key;
    }
  }
  return null;
}

function HealthPill({
  label,
  ok,
  tooltip,
}: {
  label: string;
  ok: boolean;
  tooltip: string;
}) {
  return (
    <span
      title={tooltip}
      className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] ${
        ok
          ? "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200"
          : "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
      }`}
    >
      <span
        aria-hidden
        className={`inline-block size-1.5 rounded-full ${
          ok ? "bg-emerald-500" : "bg-red-500"
        }`}
      />
      {label}
    </span>
  );
}

// Compact Issue list rows used inside the repository overview.

import { IconAlertTriangle, IconExternalLink } from "@tabler/icons-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { IssueMonitoringSnapshot } from "@/lib/api/types";

import { variantForLabel } from "@/components/agent-runner/label-variant";

interface IssueListProps {
  issues: IssueMonitoringSnapshot[];
  onSelect: (issue: IssueMonitoringSnapshot) => void;
  selectedIssueNumber: number | null;
}

export function IssueList({
  issues,
  onSelect,
  selectedIssueNumber,
}: IssueListProps) {
  return (
    <ul className="divide-y divide-slate-200 overflow-hidden rounded-md border border-slate-200 dark:divide-slate-800 dark:border-slate-800">
      {issues.map((issue) => (
        <li
          key={issue.number}
          className={cn(
            "flex items-center gap-3 bg-white px-3 py-2 text-sm transition-colors dark:bg-slate-950",
            selectedIssueNumber === issue.number
              ? "bg-slate-50 dark:bg-slate-900"
              : "hover:bg-slate-50 dark:hover:bg-slate-900",
          )}
        >
          <Button
            variant="ghost"
            size="sm"
            className="flex-1 justify-start gap-2 px-1"
            onClick={() => onSelect(issue)}
          >
            <span className="font-mono text-xs text-slate-500">#{issue.number}</span>
            <span className="truncate text-left text-slate-900 dark:text-slate-100">
              {issue.title}
            </span>
          </Button>
          <Badge variant={variantForLabel(issue.primary_label)}>
            {issue.primary_label.replace(/^agent\//, "")}
          </Badge>
          <PrSummary issue={issue} />
          <AnomalyIndicator issue={issue} />
          {issue.url ? (
            <a
              href={issue.url}
              target="_blank"
              rel="noreferrer"
              className="text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"
              aria-label={`打开 Issue #${issue.number}`}
            >
              <IconExternalLink className="size-4" />
            </a>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function PrSummary({ issue }: { issue: IssueMonitoringSnapshot }) {
  if (!issue.pr) {
    return (
      <span className="text-xs text-slate-400" title="尚未创建 PR">
        PR —
      </span>
    );
  }
  const state = describePrState(issue);
  return (
    <span
      className={cn(
        "rounded-md border px-1.5 py-0.5 text-[11px] font-mono",
        state.classes,
      )}
      title={state.tooltip}
    >
      PR #{issue.pr.number ?? "?"} {state.label}
    </span>
  );
}

function describePrState(issue: IssueMonitoringSnapshot) {
  const mergeable = issue.pr?.mergeable;
  if (mergeable === false) {
    return {
      label: "DIRTY",
      tooltip: "PR 当前不可合并，可能存在冲突。",
      classes:
        "border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200",
    };
  }
  if (issue.pr?.checks_state === "FAILURE") {
    return {
      label: "checks FAIL",
      tooltip: "PR 状态检查存在失败项。",
      classes:
        "border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200",
    };
  }
  if (issue.pr?.checks_state === "PENDING") {
    return {
      label: "checks…",
      tooltip: "PR 状态检查仍在进行。",
      classes:
        "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200",
    };
  }
  return {
    label: mergeable === true ? "clean" : "open",
    tooltip: "PR 已开启。",
    classes:
      "border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300",
  };
}

function AnomalyIndicator({ issue }: { issue: IssueMonitoringSnapshot }) {
  if (!issue.has_anomaly) {
    return null;
  }
  const hasError = issue.anomalies.some((a) => a.severity === "error");
  return (
    <span
      title={issue.anomalies.map((a) => a.message).join("\n")}
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px]",
        hasError
          ? "border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
          : "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200",
      )}
    >
      <IconAlertTriangle className="size-3" />
      {issue.anomalies.length}
    </span>
  );
}

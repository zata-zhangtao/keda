// Right-pane Issue detail view: labels, PR, worktree, event timeline, anomalies, suggested CLI.

import { IconAlertTriangle, IconCheck, IconExternalLink } from "@tabler/icons-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { IssueMonitoringSnapshot } from "@shared/api/types";

import { CopyableCommand } from "@/components/agent-runner/copyable-command";
import { prettyLabel, variantForLabel } from "@/components/agent-runner/label-variant";

interface IssueDetailProps {
  issue: IssueMonitoringSnapshot;
}

export function IssueDetail({ issue }: IssueDetailProps) {
  return (
    <div className="flex h-full flex-col gap-4 overflow-y-auto pr-1">
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle className="text-base">
              <span className="font-mono text-xs text-slate-500">
                #{issue.number}
              </span>{" "}
              {issue.title}
            </CardTitle>
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
              {issue.labels.map((label) => (
                <Badge key={label} variant={variantForLabel(label)}>
                  {prettyLabel(label)}
                </Badge>
              ))}
              {issue.url ? (
                <a
                  href={issue.url}
                  target="_blank"
                  rel="noreferrer"
                  className="ml-auto inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700"
                >
                  <IconExternalLink className="size-3" />
                  GitHub
                </a>
              ) : null}
            </div>
          </div>
        </CardHeader>
        <CardContent className="grid grid-cols-1 gap-3 text-sm md:grid-cols-2">
          <PrBlock issue={issue} />
          <WorktreeBlock issue={issue} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">事件时间线</CardTitle>
        </CardHeader>
        <CardContent>
          <TimelineList issue={issue} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">异常检测</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {issue.anomalies.length === 0 ? (
            <p className="flex items-center gap-2 text-sm text-emerald-700 dark:text-emerald-300">
              <IconCheck className="size-4" />
              当前 Issue 没有可检测到的状态异常。
            </p>
          ) : (
            issue.anomalies.map((anomaly) => (
              <div
                key={anomaly.type}
                className={cn(
                  "rounded-md border px-3 py-2 text-sm",
                  anomaly.severity === "error"
                    ? "border-red-300 bg-red-50 text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-100"
                    : "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100",
                )}
              >
                <div className="flex items-center gap-2 font-medium">
                  <IconAlertTriangle className="size-4" />
                  <span className="font-mono text-xs">{anomaly.type}</span>
                  <Badge variant={anomaly.severity}>{anomaly.severity}</Badge>
                </div>
                <p className="mt-1 text-sm">{anomaly.message}</p>
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">建议 CLI（仅可复制，监控面板不执行）</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {issue.suggested_cli_commands.length === 0 ? (
            <p className="text-sm text-slate-500">暂无推荐命令。</p>
          ) : (
            issue.suggested_cli_commands.map((command) => (
              <CopyableCommand key={command} command={command} />
            ))
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function PrBlock({ issue }: { issue: IssueMonitoringSnapshot }) {
  if (!issue.pr) {
    return (
      <div className="rounded-md border border-dashed border-slate-200 p-3 text-sm text-slate-500 dark:border-slate-800">
        <div className="font-medium text-slate-700 dark:text-slate-200">PR</div>
        <p>尚未创建或未匹配到 PR。</p>
      </div>
    );
  }
  const pr = issue.pr;
  return (
    <div className="rounded-md border border-slate-200 p-3 text-sm dark:border-slate-800">
      <div className="flex items-center justify-between font-medium text-slate-700 dark:text-slate-200">
        <span>PR #{pr.number ?? "?"}</span>
        <Badge
          variant={
            pr.mergeable === false
              ? "error"
              : pr.checks_state === "FAILURE"
                ? "error"
                : pr.checks_state === "PENDING"
                  ? "warning"
                  : "default"
          }
        >
          {pr.mergeable === false
            ? "DIRTY"
            : pr.mergeable === true
              ? "mergeable"
              : "unknown"}
        </Badge>
      </div>
      <dl className="mt-2 grid grid-cols-1 gap-1 text-xs text-slate-600 dark:text-slate-300">
        <div>
          <dt className="inline text-slate-400">branch: </dt>
          <dd className="inline font-mono">{pr.branch || "—"}</dd>
        </div>
        <div>
          <dt className="inline text-slate-400">head: </dt>
          <dd className="inline font-mono">{pr.head_sha?.slice(0, 7) || "—"}</dd>
        </div>
        <div>
          <dt className="inline text-slate-400">base: </dt>
          <dd className="inline font-mono">{pr.base_sha?.slice(0, 7) || "—"}</dd>
        </div>
        <div>
          <dt className="inline text-slate-400">checks: </dt>
          <dd className="inline">{pr.checks_state ?? "—"}</dd>
        </div>
      </dl>
      {pr.checks_summary.length > 0 ? (
        <ul className="mt-2 space-y-0.5 text-xs text-slate-500">
          {pr.checks_summary.map((line) => (
            <li key={line} className="truncate" title={line}>
              {line}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function WorktreeBlock({ issue }: { issue: IssueMonitoringSnapshot }) {
  const wt = issue.worktree;
  if (!wt.exists) {
    return (
      <div className="rounded-md border border-dashed border-slate-200 p-3 text-sm text-slate-500 dark:border-slate-800">
        <div className="font-medium text-slate-700 dark:text-slate-200">Worktree</div>
        <p>worktree 路径未找到。</p>
      </div>
    );
  }
  return (
    <div className="rounded-md border border-slate-200 p-3 text-sm dark:border-slate-800">
      <div className="flex items-center justify-between font-medium text-slate-700 dark:text-slate-200">
        <span>Worktree</span>
        <Badge variant={wt.is_clean ? "ready" : "warning"}>
          {wt.is_clean ? "clean" : "dirty"}
        </Badge>
      </div>
      <dl className="mt-2 grid grid-cols-1 gap-1 text-xs text-slate-600 dark:text-slate-300">
        <div>
          <dt className="inline text-slate-400">path: </dt>
          <dd className="inline font-mono break-all">{wt.path || "—"}</dd>
        </div>
        <div>
          <dt className="inline text-slate-400">branch: </dt>
          <dd className="inline font-mono">{wt.branch || "—"}</dd>
        </div>
        <div>
          <dt className="inline text-slate-400">head: </dt>
          <dd className="inline font-mono">{wt.head_sha?.slice(0, 7) || "—"}</dd>
        </div>
      </dl>
      {wt.dirty_files.length > 0 ? (
        <details className="mt-2 text-xs text-slate-500">
          <summary className="cursor-pointer">未跟踪变更 ({wt.dirty_files.length})</summary>
          <ul className="mt-1 space-y-0.5">
            {wt.dirty_files.map((file) => (
              <li key={file} className="font-mono">
                {file}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

function TimelineList({ issue }: { issue: IssueMonitoringSnapshot }) {
  if (issue.timeline.length === 0) {
    return (
      <p className="text-sm text-slate-500">该 Issue 还没有 iar:event marker。</p>
    );
  }
  return (
    <ol className="space-y-2">
      {issue.timeline.map((entry, index) => (
        <li
          key={`${entry.comment_index}-${entry.cycle}-${index}`}
          className="flex items-start gap-2 text-sm"
        >
          <span className="mt-1 inline-block size-1.5 shrink-0 rounded-full bg-slate-400" />
          <div className="flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs text-slate-500">
                cycle={entry.cycle}
              </span>
              <span className="font-medium text-slate-800 dark:text-slate-100">
                {entry.phase}
              </span>
              {entry.action ? (
                <Badge variant="default">{entry.action}</Badge>
              ) : null}
              {entry.mergeable === false ? (
                <Badge variant="error">mergeable=false</Badge>
              ) : null}
              {entry.checks_state ? (
                <Badge
                  variant={
                    entry.checks_state === "FAILURE"
                      ? "error"
                      : entry.checks_state === "PENDING"
                        ? "warning"
                        : "ready"
                  }
                >
                  checks={entry.checks_state}
                </Badge>
              ) : null}
            </div>
            {entry.head_sha ? (
              <div className="text-xs text-slate-500">
                head <span className="font-mono">{entry.head_sha.slice(0, 7)}</span>
                {entry.pr_branch ? (
                  <>
                    {" "}
                    · branch <span className="font-mono">{entry.pr_branch}</span>
                  </>
                ) : null}
              </div>
            ) : null}
          </div>
        </li>
      ))}
    </ol>
  );
}

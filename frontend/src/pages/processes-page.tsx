// 托管进程页：启动/停止各仓库 runner 进程，实时查看日志。
//
// 日志通过 offset 轮询续读（2.5s），仅在日志抽屉打开时轮询。

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { formatLocalDateTime } from "@/lib/utils";
import {
  fetchProcessLog,
  fetchProcesses,
  fetchRegistryRepositories,
  startProcess,
  stopProcess,
} from "@shared/api/console";
import type {
  RegistryRepositoryEntry,
  RunnerProcessKind,
  RunnerProcessRecord,
} from "@shared/api/types";

const PROCESS_POLL_INTERVAL_MS = 5000;
const LOG_POLL_INTERVAL_MS = 2500;

const KIND_LABELS: Record<RunnerProcessKind, string> = {
  daemon: "daemon（实现队列）",
  review_daemon: "review-daemon（监督）",
  run_once: "run（单轮）",
  review_once: "review（单轮）",
  blocked_continue: "blocked-continue",
};

const STARTABLE_KINDS: RunnerProcessKind[] = [
  "daemon",
  "review_daemon",
  "run_once",
  "review_once",
];

export function ProcessesPage() {
  const [processes, setProcesses] = useState<RunnerProcessRecord[]>([]);
  const [repositories, setRepositories] = useState<RegistryRepositoryEntry[]>([]);
  const [selectedRepoId, setSelectedRepoId] = useState("");
  const [selectedKind, setSelectedKind] = useState<RunnerProcessKind>("daemon");
  const [starting, setStarting] = useState(false);
  const [logProcess, setLogProcess] = useState<RunnerProcessRecord | null>(null);

  const refreshProcesses = useCallback(async () => {
    try {
      setProcesses(await fetchProcesses());
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "无法加载进程列表。",
      );
    }
  }, []);

  useEffect(() => {
    void refreshProcesses();
    const timer = setInterval(() => void refreshProcesses(), PROCESS_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [refreshProcesses]);

  useEffect(() => {
    fetchRegistryRepositories()
      .then((entries) => {
        const enabledEntries = entries.filter((entry) => entry.enabled);
        setRepositories(enabledEntries);
        if (enabledEntries.length > 0) {
          setSelectedRepoId((current) => current || enabledEntries[0].repo_id);
        }
      })
      .catch((error: unknown) => {
        toast.error(
          error instanceof Error ? error.message : "无法加载仓库列表。",
        );
      });
  }, []);

  async function handleStart() {
    if (!selectedRepoId) {
      toast.warning("请先选择目标仓库。");
      return;
    }
    const confirmed = window.confirm(
      `确认为仓库 ${selectedRepoId} 启动 ${KIND_LABELS[selectedKind]} 进程？`,
    );
    if (!confirmed) {
      return;
    }
    setStarting(true);
    try {
      const record = await startProcess({
        repo_id: selectedRepoId,
        kind: selectedKind,
      });
      toast.success(`已启动进程 ${record.process_id}（pid ${record.pid}）。`);
      await refreshProcesses();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动进程失败。");
    } finally {
      setStarting(false);
    }
  }

  async function handleStop(record: RunnerProcessRecord) {
    const confirmed = window.confirm(
      `确认停止进程 ${record.process_id}（${record.repo_id} / ${record.kind}）？`,
    );
    if (!confirmed) {
      return;
    }
    try {
      const stopped = await stopProcess(record.process_id);
      toast.success(`进程 ${record.process_id} 已 ${stopped.status}。`);
      await refreshProcesses();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "停止进程失败。");
    }
  }

  return (
    <div className="flex flex-col gap-4 p-4 lg:p-6">
      <div>
        <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
          托管进程
        </h2>
        <p className="mt-1 text-sm text-slate-500">
          每个仓库一个 daemon 进程即可获得多项目并发。进程脱离后端运行，
          后端重启不影响执行中的 runner。
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">启动新进程</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <select
            className="h-9 rounded-md border border-slate-200 bg-transparent px-2 text-sm dark:border-slate-700"
            value={selectedRepoId}
            onChange={(event) => setSelectedRepoId(event.target.value)}
            aria-label="目标仓库"
          >
            {repositories.length === 0 ? (
              <option value="">（无可用仓库）</option>
            ) : null}
            {repositories.map((entry) => (
              <option key={entry.repo_id} value={entry.repo_id}>
                {entry.display_name || entry.repo_id}
              </option>
            ))}
          </select>
          <select
            className="h-9 rounded-md border border-slate-200 bg-transparent px-2 text-sm dark:border-slate-700"
            value={selectedKind}
            onChange={(event) =>
              setSelectedKind(event.target.value as RunnerProcessKind)
            }
            aria-label="进程类型"
          >
            {STARTABLE_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {KIND_LABELS[kind]}
              </option>
            ))}
          </select>
          <Button size="sm" onClick={() => void handleStart()} disabled={starting}>
            {starting ? "启动中…" : "启动"}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">进程列表</CardTitle>
        </CardHeader>
        <CardContent>
          {processes.length === 0 ? (
            <p className="text-sm text-slate-500">暂无托管进程。</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700">
                    <th className="py-2 pr-3">仓库</th>
                    <th className="py-2 pr-3">类型</th>
                    <th className="py-2 pr-3">PID</th>
                    <th className="py-2 pr-3">状态</th>
                    <th className="py-2 pr-3">退出码</th>
                    <th className="py-2 pr-3">启动时间</th>
                    <th className="py-2 pr-3">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {processes.map((record) => (
                    <tr
                      key={record.process_id}
                      className="border-b border-slate-100 dark:border-slate-800"
                    >
                      <td className="py-2 pr-3 font-medium">{record.repo_id}</td>
                      <td className="py-2 pr-3">
                        <code className="text-xs">{record.kind}</code>
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs">{record.pid}</td>
                      <td className="py-2 pr-3">
                        <ProcessStatusBadge status={record.status} />
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs">
                        {record.exit_code ?? "—"}
                      </td>
                      <td className="py-2 pr-3 font-mono text-[11px] text-slate-500">
                        {formatLocalDateTime(record.started_at)}
                      </td>
                      <td className="py-2 pr-3">
                        <div className="flex gap-1">
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => setLogProcess(record)}
                          >
                            日志
                          </Button>
                          {record.status === "running" ? (
                            <Button
                              size="sm"
                              variant="destructive"
                              onClick={() => void handleStop(record)}
                            >
                              停止
                            </Button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <ProcessLogSheet
        record={logProcess}
        onClose={() => setLogProcess(null)}
      />
    </div>
  );
}

function ProcessStatusBadge({ status }: { status: RunnerProcessRecord["status"] }) {
  const variant =
    status === "running" ? "ready" : status === "killed" ? "warning" : "default";
  return <Badge variant={variant}>{status}</Badge>;
}

function ProcessLogSheet({
  record,
  onClose,
}: {
  record: RunnerProcessRecord | null;
  onClose: () => void;
}) {
  const [logContent, setLogContent] = useState("");
  const offsetRef = useRef(0);
  const containerRef = useRef<HTMLPreElement | null>(null);

  useEffect(() => {
    if (!record) {
      setLogContent("");
      offsetRef.current = 0;
      return;
    }
    let cancelled = false;

    async function pollLog() {
      if (!record || cancelled) {
        return;
      }
      try {
        const chunk = await fetchProcessLog(record.process_id, offsetRef.current);
        if (cancelled) {
          return;
        }
        if (chunk.content) {
          offsetRef.current = chunk.next_offset;
          setLogContent((current) => {
            const merged = current + chunk.content;
            // 防止超长日志拖垮页面，只保留尾部 ~200KB。
            return merged.length > 200_000 ? merged.slice(-200_000) : merged;
          });
        }
      } catch (error) {
        if (!cancelled) {
          toast.error(
            error instanceof Error ? error.message : "读取日志失败。",
          );
        }
      }
    }

    void pollLog();
    const timer = setInterval(() => void pollLog(), LOG_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [record]);

  useEffect(() => {
    const container = containerRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [logContent]);

  return (
    <Sheet open={record !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="w-full sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle className="text-sm">
            {record
              ? `日志：${record.repo_id} / ${record.kind}（${record.process_id}）`
              : "日志"}
          </SheetTitle>
        </SheetHeader>
        <pre
          ref={containerRef}
          className="mx-4 mb-4 h-[80vh] overflow-auto rounded-md bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-100"
        >
          {logContent || "（暂无输出）"}
        </pre>
      </SheetContent>
    </Sheet>
  );
}

// 项目页：仓库 registry 管理（列表 / 添加 / 启停）+ 审计日志。
//
// registry（config.toml 的 [agent_runner.repositories.*]）仍是事实来源，
// 本页只是它的受控编辑器；写回保留文件注释与格式。

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { formatLocalDateTime } from "@/lib/utils";
import {
  addRegistryRepository,
  fetchAuditLog,
  fetchRegistryRepositories,
  setRegistryRepositoryEnabled,
} from "@shared/api/console";
import type { AuditEntry, RegistryRepositoryEntry } from "@shared/api/types";

export function RepositoriesPage() {
  const [repositories, setRepositories] = useState<RegistryRepositoryEntry[]>([]);
  const [audits, setAudits] = useState<AuditEntry[]>([]);
  const [newRepoId, setNewRepoId] = useState("");
  const [newRepoPath, setNewRepoPath] = useState("");
  const [newDisplayName, setNewDisplayName] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setRepositories(await fetchRegistryRepositories());
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "无法加载仓库 registry。",
      );
    }
    try {
      setAudits(await fetchAuditLog(50));
    } catch {
      setAudits([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function handleAdd() {
    if (!newRepoId || !newRepoPath) {
      toast.warning("repo_id 和路径均为必填。");
      return;
    }
    setSubmitting(true);
    try {
      await addRegistryRepository({
        repo_id: newRepoId,
        path: newRepoPath,
        display_name: newDisplayName || undefined,
      });
      toast.success(`已添加仓库 ${newRepoId} 并写回 config.toml。`);
      setNewRepoId("");
      setNewRepoPath("");
      setNewDisplayName("");
      await refresh();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "添加仓库失败。");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleToggle(entry: RegistryRepositoryEntry) {
    const verb = entry.enabled ? "停用" : "启用";
    const confirmed = window.confirm(`确认${verb}仓库 ${entry.repo_id}？`);
    if (!confirmed) {
      return;
    }
    try {
      await setRegistryRepositoryEnabled(entry.repo_id, !entry.enabled);
      toast.success(`仓库 ${entry.repo_id} 已${verb}。`);
      await refresh();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : `${verb}失败。`);
    }
  }

  return (
    <div className="flex flex-col gap-4 p-4 lg:p-6">
      <div>
        <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
          项目接入
        </h2>
        <p className="mt-1 text-sm text-slate-500">
          registry 写回 <code>config.toml</code>，注释与格式保留。添加前会校验
          路径存在且为 git 仓库。
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">已注册仓库</CardTitle>
        </CardHeader>
        <CardContent>
          {repositories.length === 0 ? (
            <p className="text-sm text-slate-500">
              registry 为空。未注册仓库时，runner 默认以当前仓库为目标。
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700">
                    <th className="py-2 pr-3">repo_id</th>
                    <th className="py-2 pr-3">路径</th>
                    <th className="py-2 pr-3">显示名</th>
                    <th className="py-2 pr-3">状态</th>
                    <th className="py-2 pr-3">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {repositories.map((entry) => (
                    <tr
                      key={entry.repo_id}
                      className="border-b border-slate-100 dark:border-slate-800"
                    >
                      <td className="py-2 pr-3 font-medium">{entry.repo_id}</td>
                      <td className="py-2 pr-3 font-mono text-xs">
                        {entry.path}
                        {!entry.path_exists ? (
                          <Badge variant="warning" className="ml-2 text-[10px]">
                            路径不存在
                          </Badge>
                        ) : null}
                      </td>
                      <td className="py-2 pr-3">{entry.display_name ?? "—"}</td>
                      <td className="py-2 pr-3">
                        <Badge variant={entry.enabled ? "ready" : "default"}>
                          {entry.enabled ? "enabled" : "disabled"}
                        </Badge>
                      </td>
                      <td className="py-2 pr-3">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => void handleToggle(entry)}
                        >
                          {entry.enabled ? "停用" : "启用"}
                        </Button>
                      </td>
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
          <CardTitle className="text-sm">添加仓库</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <Input
            className="w-40"
            placeholder="repo_id（小写-连字符）"
            value={newRepoId}
            onChange={(event) => setNewRepoId(event.target.value)}
          />
          <Input
            className="w-72"
            placeholder="本地路径，如 /Users/me/code/foo"
            value={newRepoPath}
            onChange={(event) => setNewRepoPath(event.target.value)}
          />
          <Input
            className="w-40"
            placeholder="显示名（可选）"
            value={newDisplayName}
            onChange={(event) => setNewDisplayName(event.target.value)}
          />
          <Button size="sm" onClick={() => void handleAdd()} disabled={submitting}>
            {submitting ? "校验中…" : "校验并添加"}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">审计日志</CardTitle>
        </CardHeader>
        <CardContent>
          {audits.length === 0 ? (
            <p className="text-sm text-slate-500">暂无审计记录。</p>
          ) : (
            <ul className="space-y-1 text-xs">
              {audits.map((audit, index) => (
                <li
                  key={`${audit.occurred_at}-${index}`}
                  className="flex flex-wrap items-center gap-2 border-b border-slate-100 py-1.5 dark:border-slate-800"
                >
                  <span className="font-mono text-[11px] text-slate-400">
                    {formatLocalDateTime(audit.occurred_at)}
                  </span>
                  <Badge
                    variant={audit.result === "accepted" ? "ready" : "warning"}
                    className="text-[10px]"
                  >
                    {audit.result}
                  </Badge>
                  <code>{audit.action}</code>
                  {audit.repo_id ? <span>{audit.repo_id}</span> : null}
                  {audit.issue_number ? <span>#{audit.issue_number}</span> : null}
                  {audit.detail ? (
                    <span className="text-slate-500">{audit.detail}</span>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

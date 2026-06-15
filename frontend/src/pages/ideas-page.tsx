// Idea Inbox 页面：跨项目想法采集 + PRD 草稿生成 + 人审阅确认。
//
// 数据流：
// - 顶部项目选择器从 registry 拉 enabled repo。
// - 左侧为原话流（append-only），支持新增想法。
// - 中间为 AI 总结（summary.md），可手动刷新。
// - 右侧为 PRD 草稿列表，支持生成、查看、确认入 pending。
//
// 草稿只在前端确认后才落入 `tasks/pending/`，从外部 IM 来的消息只
// 进入原话流；草稿入口由 AI 在 core use case 派生。

import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchRegistryRepositories } from "@shared/api/console";
import {
  appendIdea,
  approvePrdDraft,
  createPrdDraft,
  fetchIdeaInboxMetadata,
  fetchIdeaInboxSnapshot,
  refreshIdeaSummary,
} from "@shared/api/ideaInbox";
import type {
  IdeaEntry,
  IdeaInboxMetadata,
  IdeaInboxSnapshot,
  PrdDraftSummary,
  RegistryRepositoryEntry,
} from "@shared/api/types";

const POLL_INTERVAL_MS = 30000;

export function IdeasPage() {
  const [repositories, setRepositories] = useState<RegistryRepositoryEntry[]>([]);
  const [metadata, setMetadata] = useState<IdeaInboxMetadata | null>(null);
  const [selectedRepoId, setSelectedRepoId] = useState<string>("");
  const [snapshot, setSnapshot] = useState<IdeaInboxSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [selectedIdeaIds, setSelectedIdeaIds] = useState<Set<string>>(new Set());
  const [newIdeaText, setNewIdeaText] = useState("");
  const [newIdeaAuthor, setNewIdeaAuthor] = useState("operator");
  const [summaryDraft, setSummaryDraft] = useState("");
  const [priority, setPriority] = useState("P2");
  const [prdType, setPrdType] = useState("FEAT");
  const [submittingIdea, setSubmittingIdea] = useState(false);
  const [refreshingSummary, setRefreshingSummary] = useState(false);
  const [creatingDraft, setCreatingDraft] = useState(false);
  const [approvingPath, setApprovingPath] = useState<string | null>(null);

  const enabledRepos = useMemo(
    () => repositories.filter((entry) => entry.enabled),
    [repositories],
  );

  const loadRepositories = useCallback(async () => {
    try {
      const entries = await fetchRegistryRepositories();
      setRepositories(entries);
      setSelectedRepoId((current) => {
        if (current && entries.some((e) => e.repo_id === current && e.enabled)) {
          return current;
        }
        const firstEnabled = entries.find((entry) => entry.enabled);
        return firstEnabled?.repo_id ?? "";
      });
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "无法加载仓库 registry。",
      );
    }
  }, []);

  const loadMetadata = useCallback(async () => {
    try {
      setMetadata(await fetchIdeaInboxMetadata());
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "无法加载 idea inbox 元数据。",
      );
    }
  }, []);

  const loadSnapshot = useCallback(async (repoId: string) => {
    if (!repoId) {
      setSnapshot(null);
      return;
    }
    setLoading(true);
    try {
      const loaded = await fetchIdeaInboxSnapshot(repoId);
      setSnapshot(loaded);
      setSummaryDraft((current) => current || loaded.summary_raw);
      setSelectedIdeaIds(new Set());
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "无法加载 inbox 快照。",
      );
      setSnapshot(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRepositories();
    void loadMetadata();
  }, [loadRepositories, loadMetadata]);

  useEffect(() => {
    if (!selectedRepoId) {
      return;
    }
    void loadSnapshot(selectedRepoId);
    const timer = setInterval(
      () => void loadSnapshot(selectedRepoId),
      POLL_INTERVAL_MS,
    );
    return () => clearInterval(timer);
  }, [loadSnapshot, selectedRepoId]);

  async function handleAppendIdea() {
    if (!selectedRepoId) {
      toast.warning("请先选择仓库。");
      return;
    }
    if (!newIdeaText.trim()) {
      toast.warning("想法原文不能为空。");
      return;
    }
    setSubmittingIdea(true);
    try {
      await appendIdea({
        repoId: selectedRepoId,
        text: newIdeaText,
        author: newIdeaAuthor || "operator",
      });
      setNewIdeaText("");
      toast.success("已追加到想法原话流。");
      await loadSnapshot(selectedRepoId);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "追加想法失败。");
    } finally {
      setSubmittingIdea(false);
    }
  }

  async function handleRefreshSummary() {
    if (!selectedRepoId) {
      return;
    }
    setRefreshingSummary(true);
    try {
      await refreshIdeaSummary({
        repoId: selectedRepoId,
        summaryText: summaryDraft || snapshot?.summary_raw || "",
        sourceLabel: "agent",
      });
      toast.success("已重写 summary.md。");
      await loadSnapshot(selectedRepoId);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "刷新 summary 失败。",
      );
    } finally {
      setRefreshingSummary(false);
    }
  }

  async function handleCreateDraft() {
    if (!selectedRepoId) {
      return;
    }
    if (selectedIdeaIds.size === 0) {
      toast.warning("请先勾选至少一条想法。");
      return;
    }
    setCreatingDraft(true);
    try {
      await createPrdDraft({
        repoId: selectedRepoId,
        ideaRefs: Array.from(selectedIdeaIds),
        priority,
        prdType,
      });
      toast.success("已生成 PRD 草稿。");
      await loadSnapshot(selectedRepoId);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "生成草稿失败。");
    } finally {
      setCreatingDraft(false);
    }
  }

  async function handleApproveDraft(draft: PrdDraftSummary) {
    if (!selectedRepoId) {
      return;
    }
    const confirmed = window.confirm(
      `确认将草稿「${draft.title}」写入 tasks/pending/？\n\n草稿 ID: ${draft.metadata.draft_id}\n目标文件: tasks/pending/<PRIORITY>-<TYPE>-<draft_id>-<slug>.md`,
    );
    if (!confirmed) {
      return;
    }
    setApprovingPath(draft.draft_path);
    try {
      const result = await approvePrdDraft({
        repoId: selectedRepoId,
        draftPath: draft.draft_path,
        priority: draft.metadata.priority,
        prdType: draft.metadata.prd_type,
      });
      toast.success(`草稿已确认，目标：${result.pending_path}`);
      await loadSnapshot(selectedRepoId);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "确认草稿失败。");
    } finally {
      setApprovingPath(null);
    }
  }

  function toggleIdeaSelection(entry: IdeaEntry) {
    setSelectedIdeaIds((current) => {
      const next = new Set(current);
      if (next.has(entry.entry_id)) {
        next.delete(entry.entry_id);
      } else {
        next.add(entry.entry_id);
      }
      return next;
    });
  }

  return (
    <div className="flex flex-col gap-4 p-4 lg:p-6">
      <div>
        <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
          Idea Inbox
        </h2>
        <p className="mt-1 text-sm text-slate-500">
          跨项目采集想法，由 AI 生成 PRD 草稿，人确认后落入{" "}
          <code>tasks/pending/</code>。
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">仓库与操作</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-3">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm">
                仓库：{selectedRepoId || "未选择"}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuRadioGroup
                value={selectedRepoId}
                onValueChange={setSelectedRepoId}
              >
                {enabledRepos.length === 0 && (
                  <DropdownMenuRadioItem value="" disabled>
                    （无可用仓库）
                  </DropdownMenuRadioItem>
                )}
                {enabledRepos.map((entry) => (
                  <DropdownMenuRadioItem
                    key={entry.repo_id}
                    value={entry.repo_id}
                  >
                    {entry.display_name || entry.repo_id}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <div className="flex flex-1 flex-wrap items-center gap-2">
            <span className="text-xs text-slate-500">草稿 priority</span>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm">
                  {priority}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                <DropdownMenuRadioGroup
                  value={priority}
                  onValueChange={setPriority}
                >
                  {(metadata?.priorities ?? ["P0", "P1", "P2", "P3"]).map(
                    (value) => (
                      <DropdownMenuRadioItem key={value} value={value}>
                        {value}
                      </DropdownMenuRadioItem>
                    ),
                  )}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
            <span className="text-xs text-slate-500">草稿 type</span>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm">
                  {prdType}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                <DropdownMenuRadioGroup value={prdType} onValueChange={setPrdType}>
                  {(metadata?.prd_types ?? ["FEAT", "BUG", "CHORE"]).map(
                    (value) => (
                      <DropdownMenuRadioItem key={value} value={value}>
                        {value}
                      </DropdownMenuRadioItem>
                    ),
                  )}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
            <Button
              size="sm"
              onClick={() => void handleCreateDraft()}
              disabled={
                creatingDraft ||
                !selectedRepoId ||
                selectedIdeaIds.size === 0
              }
            >
              {creatingDraft ? "生成中…" : "从选中想法生成草稿"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">
              原话日志（append-only）
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <div className="flex flex-col gap-2">
              <Input
                placeholder="操作者（默认 operator）"
                value={newIdeaAuthor}
                onChange={(event) => setNewIdeaAuthor(event.target.value)}
              />
              <textarea
                aria-label="新想法"
                className="min-h-24 w-full rounded-md border border-slate-200 bg-white p-2 text-sm shadow-sm focus:border-slate-400 focus:outline-none dark:border-slate-800 dark:bg-slate-950"
                placeholder="记录一条新想法…"
                value={newIdeaText}
                onChange={(event) => setNewIdeaText(event.target.value)}
              />
              <Button
                size="sm"
                onClick={() => void handleAppendIdea()}
                disabled={submittingIdea || !selectedRepoId}
              >
                {submittingIdea ? "追加中…" : "追加到 inbox"}
              </Button>
            </div>
            <div className="flex flex-col gap-2">
              {loading ? (
                <Skeleton className="h-24" />
              ) : !snapshot || snapshot.entries.length === 0 ? (
                <p className="text-sm text-slate-500">
                  该仓库暂未收录任何想法。
                </p>
              ) : (
                snapshot.entries.map((entry) => (
                  <label
                    key={entry.entry_id}
                    className="flex cursor-pointer items-start gap-2 rounded border border-slate-200 p-2 text-sm dark:border-slate-800"
                  >
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={selectedIdeaIds.has(entry.entry_id)}
                      onChange={() => toggleIdeaSelection(entry)}
                    />
                    <div className="flex flex-col">
                      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                        <span>{entry.occurred_at}</span>
                        <Badge variant="secondary">{entry.source}</Badge>
                        <span>· {entry.author}</span>
                      </div>
                      <p className="mt-1 whitespace-pre-wrap text-slate-800 dark:text-slate-100">
                        {entry.text}
                      </p>
                    </div>
                  </label>
                ))
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">AI 总结</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <p className="text-xs text-slate-500">
              事实来源是 <code>tasks/inbox/ideas.md</code>；summary.md
              为 AI 派生可重写。
            </p>
            <textarea
              aria-label="summary"
              className="min-h-40 w-full rounded-md border border-slate-200 bg-white p-2 text-sm shadow-sm focus:border-slate-400 focus:outline-none dark:border-slate-800 dark:bg-slate-950"
              value={summaryDraft}
              onChange={(event) => setSummaryDraft(event.target.value)}
              placeholder={
                snapshot?.summary_raw
                  ? ""
                  : "选择仓库后这里会展示 AI 总结，可编辑后保存。"
              }
            />
            <Button
              size="sm"
              variant="outline"
              onClick={() => void handleRefreshSummary()}
              disabled={refreshingSummary || !selectedRepoId}
            >
              {refreshingSummary ? "刷新中…" : "重写 summary.md"}
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">PRD 草稿</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {loading ? (
              <Skeleton className="h-24" />
            ) : !snapshot || snapshot.drafts.length === 0 ? (
              <p className="text-sm text-slate-500">
                暂未生成草稿。选择想法后点击「从选中想法生成草稿」。
              </p>
            ) : (
              snapshot.drafts.map((draft) => (
                <div
                  key={draft.draft_path}
                  className="flex flex-col gap-1 rounded border border-slate-200 p-2 text-sm dark:border-slate-800"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      variant={
                        draft.metadata.status === "approved"
                          ? "default"
                          : "outline"
                      }
                    >
                      {draft.metadata.status}
                    </Badge>
                    <span className="text-xs text-slate-500">
                      {draft.metadata.priority} · {draft.metadata.prd_type}
                    </span>
                    <span className="ml-auto text-xs text-slate-500">
                      {draft.metadata.draft_id}
                    </span>
                  </div>
                  <p className="font-medium text-slate-800 dark:text-slate-100">
                    {draft.title}
                  </p>
                  <p className="whitespace-pre-wrap text-xs text-slate-500">
                    {draft.body_excerpt}
                  </p>
                  {draft.metadata.approved_pending_path && (
                    <p className="text-xs text-slate-500">
                      目标：<code>{draft.metadata.approved_pending_path}</code>
                    </p>
                  )}
                  {draft.metadata.status === "pending-review" && (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => void handleApproveDraft(draft)}
                      disabled={approvingPath === draft.draft_path}
                    >
                      {approvingPath === draft.draft_path
                        ? "确认中…"
                        : "确认入 pending"}
                    </Button>
                  )}
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

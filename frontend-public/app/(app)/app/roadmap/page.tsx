"use client"
/* eslint-disable react-hooks/set-state-in-effect */

// 路线图页面：展示 PRD 全景、依赖与批量启动能力。

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { RoadmapList } from "@/components/roadmap/roadmap-list";
import { RoadmapTimeline } from "@/components/roadmap/roadmap-timeline";
import { fetchRegistryRepositories } from "@/lib/api/console";
import {
  fetchRoadmapPrds,
  fetchRoadmapSettings,
  startGlobalRoadmap,
  startRoadmapPrd,
  stopGlobalRoadmap,
  updateRoadmapSettings,
} from "@/lib/api/roadmap";
import type {
  RegistryRepositoryEntry,
  RoadmapPrd,
  RoadmapSettings,
} from "@/lib/api/types";

const POLL_INTERVAL_MS = 30000;

type RoadmapView = "timeline" | "list";

const VIEW_LABELS: Record<RoadmapView, string> = {
  timeline: "时间轴",
  list: "列表",
};

export default function RoadmapPage() {
  const [prds, setPrds] = useState<RoadmapPrd[]>([]);
  const [loading, setLoading] = useState(true);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selectedRepoId, setSelectedRepoId] = useState("");
  const [repositories, setRepositories] = useState<RegistryRepositoryEntry[]>([]);
  const [reposLoading, setReposLoading] = useState(true);
  const [settings, setSettings] = useState<RoadmapSettings | null>(null);
  const [view, setView] = useState<RoadmapView>("list");
  const [startingPath, setStartingPath] = useState<string | null>(null);
  const [globalStarting, setGlobalStarting] = useState(false);

  const loadData = useCallback(async () => {
    if (!selectedRepoId) {
      return;
    }
    try {
      const response = await fetchRoadmapPrds({
        repoId: selectedRepoId,
        includeArchived,
      });
      setPrds(response.prds);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载路线图失败。");
    }
  }, [selectedRepoId, includeArchived]);

  useEffect(() => {
    setReposLoading(true);
    fetchRegistryRepositories()
      .then((loadedRepositories) => {
        const enabledRepositories = loadedRepositories.filter((repo) => repo.enabled);
        setRepositories(enabledRepositories);
        if (enabledRepositories.length === 0) {
          return;
        }
        const preferred =
          enabledRepositories.find((repo) => repo.repo_id === "keda-main") ??
          enabledRepositories[0];
        setSelectedRepoId((current) => current || preferred.repo_id);
      })
      .catch((error: unknown) => {
        toast.error(error instanceof Error ? error.message : "加载仓库列表失败。");
      })
      .finally(() => setReposLoading(false));
  }, []);

  useEffect(() => {
    if (!selectedRepoId) {
      return;
    }
    setLoading(true);
    void loadData().finally(() => setLoading(false));
    const timer = setInterval(() => void loadData(), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [loadData, selectedRepoId]);

  useEffect(() => {
    if (!selectedRepoId) {
      return;
    }
    fetchRoadmapSettings(selectedRepoId)
      .then((loadedSettings) => {
        setSettings(loadedSettings);
        setView(loadedSettings.default_view);
      })
      .catch((error: unknown) => {
        toast.error(error instanceof Error ? error.message : "加载设置失败。");
      });
  }, [selectedRepoId]);

  async function handleViewChange(nextView: RoadmapView) {
    setView(nextView);
    if (!settings || settings.default_view === nextView) {
      return;
    }
    try {
      const updated = await updateRoadmapSettings({
        repoId: selectedRepoId,
        maxParallel: settings.max_parallel,
        defaultView: nextView,
      });
      setSettings(updated);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存视图设置失败。");
    }
  }

  async function handleStart(prd: RoadmapPrd) {
    setStartingPath(prd.prd_path);
    try {
      await startRoadmapPrd(selectedRepoId, prd.prd_path);
      toast.success(`${prd.title} 已开始。`);
      await loadData();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动 PRD 失败。");
    } finally {
      setStartingPath(null);
    }
  }

  async function handleStartGlobal() {
    if (!settings) {
      toast.warning("设置尚未加载。");
      return;
    }
    setGlobalStarting(true);
    try {
      const result = await startGlobalRoadmap({
        repoId: selectedRepoId,
        maxParallel: settings.max_parallel,
      });
      toast.success(
        `全局开始完成：启动 ${result.started.length} 个，排队 ${result.queued.length} 个。`,
      );
      await loadData();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "全局开始失败。");
    } finally {
      setGlobalStarting(false);
    }
  }

  async function handleStopGlobal() {
    try {
      await stopGlobalRoadmap(selectedRepoId);
      toast.success("已停止全局调度。");
      await loadData();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "停止全局调度失败。");
    }
  }

  const visiblePrds = includeArchived
    ? prds
    : prds.filter((prd) => prd.status === "pending");

  return (
    <div className="flex flex-col gap-4 p-4 lg:p-6">
      <div>
        <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
          路线图
        </h2>
        <p className="mt-1 text-sm text-slate-500">
          查看 pending/archived PRD 的状态、依赖关系，并批量启动开发。
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">控制面板</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-3">
          <select
            className="h-9 w-48 rounded-md border border-slate-200 bg-transparent px-2 text-sm dark:border-slate-700"
            value={selectedRepoId}
            onChange={(event) => setSelectedRepoId(event.target.value)}
            disabled={reposLoading || repositories.length === 0}
            aria-label="选择仓库"
          >
            {repositories.length === 0 ? (
              <option value="">{reposLoading ? "加载中…" : "无可用仓库"}</option>
            ) : (
              repositories.map((repo) => (
                <option key={repo.repo_id} value={repo.repo_id}>
                  {repo.display_name ?? repo.repo_id}
                </option>
              ))
            )}
          </select>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(event) => setIncludeArchived(event.target.checked)}
            />
            显示已归档
          </label>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm">
                视图：{VIEW_LABELS[view]}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuRadioGroup
                value={view}
                onValueChange={(value) => void handleViewChange(value as RoadmapView)}
              >
                <DropdownMenuRadioItem value="timeline">时间轴</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="list">列表</DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <div className="flex-1" />
          <Button
            size="sm"
            onClick={() => void handleStartGlobal()}
            disabled={globalStarting || !settings}
          >
            {globalStarting ? "全局启动中…" : "全局开始"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => void handleStopGlobal()}
            disabled={globalStarting}
          >
            停止全局调度
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">
            {VIEW_LABELS[view]}（{visiblePrds.length}）
          </CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
              <Skeleton className="h-40" />
              <Skeleton className="h-40" />
              <Skeleton className="h-40" />
            </div>
          ) : view === "timeline" ? (
            <RoadmapTimeline
              prds={visiblePrds}
              onStart={(prd) => void handleStart(prd)}
              startingPath={startingPath}
            />
          ) : (
            <RoadmapList
              prds={visiblePrds}
              onStart={(prd) => void handleStart(prd)}
              startingPath={startingPath}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

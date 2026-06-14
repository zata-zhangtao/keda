import { PrdCard } from "./prd-card";
import type { RoadmapPrd } from "@shared/api/types";

interface RoadmapTimelineProps {
  prds: RoadmapPrd[];
  onStart: (prd: RoadmapPrd) => void;
  startingPath: string | null;
}

function buildTopologicalLevels(prds: RoadmapPrd[]): RoadmapPrd[][] {
  const prdMap = new Map(prds.map((prd) => [prd.prd_path, prd]));
  const inDegree = new Map<string, number>();
  const outgoing = new Map<string, string[]>();

  for (const prd of prds) {
    inDegree.set(prd.prd_path, 0);
    outgoing.set(prd.prd_path, []);
  }

  for (const prd of prds) {
    for (const dep of prd.delivery_dependencies) {
      if (dep.kind === "prd" && prdMap.has(dep.to_path)) {
        outgoing.get(dep.to_path)?.push(prd.prd_path);
        inDegree.set(prd.prd_path, (inDegree.get(prd.prd_path) ?? 0) + 1);
      }
    }
  }

  const queue: string[] = [];
  const levelMap = new Map<string, number>();
  for (const [path, degree] of inDegree) {
    if (degree === 0) {
      queue.push(path);
      levelMap.set(path, 0);
    }
  }

  while (queue.length > 0) {
    const current = queue.shift()!;
    const currentLevel = levelMap.get(current) ?? 0;
    for (const next of outgoing.get(current) ?? []) {
      const nextLevel = levelMap.get(next) ?? 0;
      levelMap.set(next, Math.max(nextLevel, currentLevel + 1));
      const degree = (inDegree.get(next) ?? 0) - 1;
      inDegree.set(next, degree);
      if (degree === 0) {
        queue.push(next);
      }
    }
  }

  const maxLevel = Math.max(0, ...levelMap.values());
  const levels: RoadmapPrd[][] = [];
  for (let index = 0; index <= maxLevel; index++) {
    levels.push([]);
  }
  for (const prd of prds) {
    const level = levelMap.get(prd.prd_path) ?? 0;
    levels[level].push(prd);
  }
  return levels.filter((level) => level.length > 0);
}

export function RoadmapTimeline({ prds, onStart, startingPath }: RoadmapTimelineProps) {
  if (prds.length === 0) {
    return <p className="text-sm text-slate-500">暂无 PRD。</p>;
  }

  const levels = buildTopologicalLevels(prds);

  return (
    <div className="space-y-8">
      {levels.map((level, levelIndex) => (
        <div key={levelIndex} className="relative">
          <div className="mb-2 text-xs font-medium text-slate-400">
            阶段 {levelIndex + 1}
          </div>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
            {level.map((prd) => (
              <PrdCard
                key={prd.prd_path}
                prd={prd}
                onStart={() => onStart(prd)}
                starting={startingPath === prd.prd_path}
              />
            ))}
          </div>
          {levelIndex < levels.length - 1 ? (
            <div className="mt-4 flex justify-center">
              <div className="h-6 w-px bg-slate-300 dark:bg-slate-700" />
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}

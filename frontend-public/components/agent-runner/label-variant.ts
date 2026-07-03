// Maps a workflow `agent/*` label to a UI badge variant.
import type { BadgeVariant } from "@/components/ui/badge";

const LABEL_TO_VARIANT: Record<string, BadgeVariant> = {
  "agent/ready": "ready",
  "agent/running": "running",
  "agent/supervising": "supervising",
  "agent/review": "review",
  "agent/failed": "failed",
  "agent/blocked": "blocked",
};

export function variantForLabel(label: string): BadgeVariant {
  return LABEL_TO_VARIANT[label] ?? "default";
}

export function prettyLabel(label: string): string {
  if (label.startsWith("agent/")) {
    return label.slice("agent/".length);
  }
  return label;
}

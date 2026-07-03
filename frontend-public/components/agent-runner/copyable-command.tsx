// Displays a single suggested CLI command with a copy-to-clipboard affordance.
// No "execute" button — recovery actions live in the CLI by design.

import { useState } from "react";

import { cn } from "@/lib/utils";

interface CopyableCommandProps {
  command: string;
  className?: string;
}

export function CopyableCommand({ command, className }: CopyableCommandProps) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle");

  async function handleCopy() {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(command);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = command;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      setCopyState("copied");
      setTimeout(() => setCopyState("idle"), 1500);
    } catch {
      setCopyState("error");
      setTimeout(() => setCopyState("idle"), 2000);
    }
  }

  const buttonLabel =
    copyState === "copied"
      ? "已复制"
      : copyState === "error"
        ? "复制失败"
        : "复制";

  return (
    <div
      className={cn(
        "flex items-center justify-between gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-1.5 font-mono text-xs text-slate-800 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-100",
        className,
      )}
    >
      <code className="flex-1 truncate" title={command}>
        {command}
      </code>
      <button
        type="button"
        onClick={handleCopy}
        className="shrink-0 rounded-md border border-slate-300 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-700 transition-colors hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200 dark:hover:bg-slate-700"
        aria-label={`复制命令: ${command}`}
      >
        {buttonLabel}
      </button>
    </div>
  );
}

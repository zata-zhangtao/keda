import * as React from "react";

import { cn } from "@/lib/utils";

export interface ProgressProps extends React.ComponentProps<"div"> {
  value?: number;
}

export function Progress({ className, value = 0, ...props }: ProgressProps) {
  const clamped = Math.min(100, Math.max(0, value));
  return (
    <div
      data-slot="progress"
      className={cn(
        "relative h-2 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800",
        className,
      )}
      {...props}
    >
      <div
        className="h-full rounded-full bg-blue-600 transition-all dark:bg-blue-500"
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}

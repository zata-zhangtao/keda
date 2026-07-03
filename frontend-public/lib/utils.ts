import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

/** Merge Tailwind classes with conditional class names. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// 将 ISO 8601 UTC 时间字符串格式化为本地系统时间（yyyy-MM-dd HH:mm:ss）。
export function formatLocalDateTime(isoString: string | null | undefined): string {
  if (!isoString) {
    return "—"
  }
  const parsedDate = new Date(isoString)
  if (Number.isNaN(parsedDate.getTime())) {
    return isoString
  }

  const pad = (value: number) => String(value).padStart(2, "0")
  const year = parsedDate.getFullYear()
  const month = pad(parsedDate.getMonth() + 1)
  const day = pad(parsedDate.getDate())
  const hour = pad(parsedDate.getHours())
  const minute = pad(parsedDate.getMinutes())
  const second = pad(parsedDate.getSeconds())
  return `${year}-${month}-${day} ${hour}:${minute}:${second}`
}

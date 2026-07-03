/**
 * Safe localStorage wrapper for typed JSON values.
 *
 * All operations are no-ops when `window.localStorage` is unavailable
 * (SSR, sandboxed iframes, disabled storage) and swallow quota/parse
 * errors so a single bad value cannot break the host app.
 *
 * The `v1:` prefix gives a free migration knob if the schema ever changes.
 */

const KEY_PREFIX = 'v1:'

/** Check whether `localStorage` can be used in the current environment. */
function isAvailable(): boolean {
  try {
    return typeof window !== 'undefined' && !!window.localStorage
  } catch {
    return false
  }
}

/** Read a JSON-serialized value, returning `null` if missing or unparseable. */
export function getItem<T>(key: string): T | null {
  if (!isAvailable()) return null
  try {
    const raw = window.localStorage.getItem(KEY_PREFIX + key)
    if (raw === null) return null
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

/** Write a value as JSON, silently ignoring quota or serialization errors. */
export function setItem<T>(key: string, value: T): void {
  if (!isAvailable()) return
  try {
    window.localStorage.setItem(KEY_PREFIX + key, JSON.stringify(value))
  } catch {
    /* quota exceeded / disabled storage / circular ref */
  }
}

/** Remove a key, silently ignoring errors. */
export function removeItem(key: string): void {
  if (!isAvailable()) return
  try {
    window.localStorage.removeItem(KEY_PREFIX + key)
  } catch {
    /* */
  }
}

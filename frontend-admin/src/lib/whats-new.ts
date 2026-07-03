/**
 * "What's New" detection: loads the build-time manifest, compares it
 * against the user's last-seen version in localStorage, and returns a
 * decision that the host component can render directly.
 *
 * Staging builds never auto-show the modal; the version is still embedded
 * in the manifest for debugging but the UI stays quiet.
 */

import { getItem, setItem } from './storage'

const LAST_SEEN_KEY = 'whats-new:last-seen'
const SESSION_FLAG_KEY = 'whats-new:shown-this-session'

export type WhatsNewPayload = {
  version: string
  mode: 'production' | 'staging'
  previousVersion: string | null
  generatedAt: string
  groups: Record<string, string[]>
  breaking: string[]
}

export type WhatsNewDecision =
  | { kind: 'show'; payload: WhatsNewPayload }
  | { kind: 'skip'; reason: WhatsNewSkipReason }

export type WhatsNewSkipReason =
  | 'no-manifest'
  | 'no-version'
  | 'up-to-date'
  | 'session-already-shown'

/** Fetch the manifest produced at build time. Returns null on any failure. */
export async function loadManifest(
  fetchImpl: typeof fetch = fetch,
  url = '/versions.json'
): Promise<WhatsNewPayload | null> {
  try {
    const response = await fetchImpl(url, { cache: 'no-cache' })
    if (!response.ok) return null
    const json: unknown = await response.json()
    if (!isPayload(json)) return null
    return json
  } catch {
    return null
  }
}

/** Pure decision: should the modal show for the given payload? */
export function shouldShow(payload: WhatsNewPayload | null): WhatsNewDecision {
  if (payload === null) {
    return { kind: 'skip', reason: 'no-manifest' }
  }
  if (payload.mode !== 'production' || !payload.version) {
    return { kind: 'skip', reason: 'no-version' }
  }
  const lastSeen = getItem<string>(LAST_SEEN_KEY)
  if (lastSeen === payload.version) {
    return { kind: 'skip', reason: 'up-to-date' }
  }
  if (sessionSeen(payload.version)) {
    return { kind: 'skip', reason: 'session-already-shown' }
  }
  return { kind: 'show', payload }
}

/** Mark the current version as seen (persists across reloads). */
export function markSeen(version: string): void {
  setItem(LAST_SEEN_KEY, version)
  try {
    window.sessionStorage.setItem(SESSION_FLAG_KEY, version)
  } catch {
    /* session storage unavailable */
  }
}

/** Check whether the current session has already seen the given version. */
function sessionSeen(version: string): boolean {
  try {
    return window.sessionStorage.getItem(SESSION_FLAG_KEY) === version
  } catch {
    return false
  }
}

/** Type guard for a valid whats-new payload object. */
function isPayload(value: unknown): value is WhatsNewPayload {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  return (
    typeof record['version'] === 'string' &&
    typeof record['mode'] === 'string' &&
    typeof record['generatedAt'] === 'string' &&
    typeof record['groups'] === 'object' &&
    record['groups'] !== null &&
    Array.isArray(record['breaking'])
  )
}

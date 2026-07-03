import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  loadManifest,
  markSeen,
  shouldShow,
  type WhatsNewPayload,
} from './whats-new'

const STORAGE_KEY = 'v1:whats-new:last-seen'
const SESSION_KEY = 'whats-new:shown-this-session'

const PRODUCTION_PAYLOAD: WhatsNewPayload = {
  version: 'v1.2.3',
  mode: 'production',
  previousVersion: 'v1.2.2',
  generatedAt: '2026-06-15T00:00:00.000Z',
  groups: {
    Features: ['add whats-new modal (frontend-admin)'],
    'Bug Fixes': [],
    Performance: [],
    Refactors: [],
    Reverts: [],
    Maintenance: [],
  },
  breaking: [],
}

const STAGING_PAYLOAD: WhatsNewPayload = {
  ...PRODUCTION_PAYLOAD,
  mode: 'staging',
  version: 'abc1234',
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('shouldShow', () => {
  it('skips when there is no manifest', () => {
    expect(shouldShow(null)).toEqual({ kind: 'skip', reason: 'no-manifest' })
  })

  it('skips staging builds even when payload is present', () => {
    const decision = shouldShow(STAGING_PAYLOAD)
    expect(decision).toEqual({ kind: 'skip', reason: 'no-version' })
  })

  it('shows the modal for a fresh production version', () => {
    const decision = shouldShow(PRODUCTION_PAYLOAD)
    expect(decision).toEqual({ kind: 'show', payload: PRODUCTION_PAYLOAD })
  })

  it('skips when the version matches the last-seen one', () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify('v1.2.3'))
    expect(shouldShow(PRODUCTION_PAYLOAD)).toEqual({
      kind: 'skip',
      reason: 'up-to-date',
    })
  })

  it('skips when the version was already shown in this session', () => {
    window.sessionStorage.setItem(SESSION_KEY, 'v1.2.3')
    expect(shouldShow(PRODUCTION_PAYLOAD)).toEqual({
      kind: 'skip',
      reason: 'session-already-shown',
    })
  })
})

describe('markSeen', () => {
  it('persists the version to localStorage and sessionStorage', () => {
    markSeen('v1.2.3')
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('"v1.2.3"')
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBe('v1.2.3')
  })
})

describe('loadManifest', () => {
  it('returns the parsed payload on a 200 response', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(PRODUCTION_PAYLOAD),
    })
    const result = await loadManifest(fetchImpl as unknown as typeof fetch)
    expect(result).toEqual(PRODUCTION_PAYLOAD)
  })

  it('returns null on a non-2xx response', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, json: () => Promise.resolve({}) })
    const result = await loadManifest(fetchImpl as unknown as typeof fetch)
    expect(result).toBeNull()
  })

  it('returns null when the JSON does not match the payload shape', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ version: 42 }),
    })
    const result = await loadManifest(fetchImpl as unknown as typeof fetch)
    expect(result).toBeNull()
  })

  it('returns null when the fetch itself rejects', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error('network'))
    const result = await loadManifest(fetchImpl as unknown as typeof fetch)
    expect(result).toBeNull()
  })
})

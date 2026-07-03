import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render } from 'vitest-browser-react'
import { userEvent } from 'vitest/browser'

import { markSeen, type WhatsNewPayload } from '@/lib/whats-new'

import { WhatsNewDialog } from './whats-new-dialog'

vi.mock('@/lib/whats-new', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/whats-new')>()
  return {
    ...actual,
    markSeen: vi.fn(),
  }
})

const markSeenMock = vi.mocked(markSeen)

const basePayload: WhatsNewPayload = {
  version: 'v1.2.3',
  mode: 'production',
  previousVersion: 'v1.2.2',
  generatedAt: '2026-06-15T00:00:00.000Z',
  groups: {
    Features: ['add whats-new modal (frontend-admin)'],
    'Bug Fixes': ['fix login flash (auth)'],
    Performance: [],
    Refactors: [],
    Reverts: [],
    Maintenance: [],
  },
  breaking: [],
}

beforeEach(() => {
  markSeenMock.mockReset()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('WhatsNewDialog', () => {
  it('renders the version and each non-empty group', async () => {
    await render(<WhatsNewDialog payload={basePayload} />)

    await expect
      .element(
        document.querySelector(
          '[data-slot="dialog-title"]'
        ) as HTMLElement | null
      )
      .toHaveTextContent("What's New in v1.2.3")

    const featuresSection = document.querySelector(
      '[data-testid="whats-new-group-features"]'
    )
    const bugFixesSection = document.querySelector(
      '[data-testid="whats-new-group-bug-fixes"]'
    )
    expect(featuresSection).toBeTruthy()
    expect(featuresSection?.textContent).toContain('add whats-new modal')
    expect(bugFixesSection).toBeTruthy()
    expect(bugFixesSection?.textContent).toContain('fix login flash')
  })

  it('renders the breaking-changes section when present', async () => {
    const payload: WhatsNewPayload = {
      ...basePayload,
      breaking: ['drop v1 endpoints', 'rename /users to /accounts'],
    }
    await render(<WhatsNewDialog payload={payload} />)

    const breakingSection = document.querySelector(
      '[data-testid="whats-new-breaking"]'
    )
    expect(breakingSection).toBeTruthy()
    expect(breakingSection?.textContent).toContain('drop v1 endpoints')
    expect(breakingSection?.textContent).toContain('rename /users to /accounts')
  })

  it('calls markSeen with the version when the user dismisses', async () => {
    await render(<WhatsNewDialog payload={basePayload} />)

    await userEvent.click(
      document.querySelector('[data-slot="dialog-close"]') as HTMLElement
    )

    expect(markSeenMock).toHaveBeenCalledWith('v1.2.3')
  })

  it('falls back to the "no notable changes" copy when nothing is documented', async () => {
    const emptyPayload: WhatsNewPayload = {
      ...basePayload,
      groups: {
        Features: [],
        'Bug Fixes': [],
        Performance: [],
        Refactors: [],
        Reverts: [],
        Maintenance: [],
      },
      breaking: [],
    }
    await render(<WhatsNewDialog payload={emptyPayload} />)

    await expect
      .element(document.body)
      .toHaveTextContent('No notable changes documented for this release.')
  })
})

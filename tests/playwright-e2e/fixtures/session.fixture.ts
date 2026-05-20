import { test as base, expect } from '@playwright/test'
import { ApiClient } from '../support/api-client'
import { CleanupRegistry } from '../support/cleanup'

type SessionFixtures = {
  /** Authenticated API client for seeding / cleanup. */
  api: ApiClient
  /** Registry that auto-runs cleanup after each test. */
  cleanupRegistry: CleanupRegistry
  // TODO: add page-object fixtures here, e.g.:
  // homePage: HomePage
}

/**
 * Extends the base Playwright test with an authenticated API client and a
 * cleanup registry.  Import this instead of '@playwright/test' in all tests
 * that need a session.
 *
 * Usage:
 *   import { test, expect } from '../fixtures/session.fixture'
 */
export const test = base.extend<SessionFixtures>({
  api: async ({}, use) => {
    const client = await ApiClient.createAuthenticated()
    try {
      await use(client)
    } finally {
      await client.dispose()
    }
  },

  cleanupRegistry: async ({ api }, use) => {
    const registry = new CleanupRegistry()
    try {
      await use(registry)
    } finally {
      await registry.runAll()
      void api  // keeps the fixture dependency alive through cleanup
    }
  },

  // TODO: add page-object fixtures here, e.g.:
  // homePage: async ({ page }, use) => { await use(new HomePage(page)) },
})

export { expect }

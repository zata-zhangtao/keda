import { test as setup, expect } from '@playwright/test'
import { ensureAuthDirectory, getAuthStorageStatePath } from '../../support/env'

/**
 * Auth setup step — runs once before all 'chromium' project tests.
 *
 * The backend uses local single-operator auth: any visitor is treated as the
 * local operator. We navigate to the dashboard, wait for it to render, and
 * persist the storage state so subsequent tests can reuse the session.
 */
setup('authenticate and persist storage state', async ({ page }) => {
  ensureAuthDirectory()

  await page.goto('/dashboard')
  await expect(page.getByRole('main')).toBeVisible()
  await expect(page).toHaveURL(/\/dashboard/)

  await page.context().storageState({ path: getAuthStorageStatePath() })
})

import { test as setup, expect } from '@playwright/test'
import { LoginPage } from '../../page-objects/LoginPage'
import { ensureAuthDirectory, getAuthStorageStatePath, getCredentials } from '../../support/env'

/**
 * Auth setup step — runs once before all 'chromium' project tests.
 * Logs in with the bootstrap credentials and persists cookies / localStorage
 * to .auth/session.json so tests don't need to re-authenticate.
 */
setup('authenticate and persist storage state', async ({ page }) => {
  ensureAuthDirectory()

  const loginPage = new LoginPage(page)

  // TODO: replace with your app's protected entry-point URL
  await page.goto('/dashboard')
  await loginPage.waitForReady()
  await loginPage.login(getCredentials())

  // TODO: replace with a post-login assertion that confirms the redirect worked
  await expect(page).toHaveURL(/\/dashboard/)

  await page.context().storageState({ path: getAuthStorageStatePath() })
})

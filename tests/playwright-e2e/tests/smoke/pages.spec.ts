/**
 * Smoke tests — verify that key pages load without JS errors.
 * These run with full auth (chromium project).
 */
import { expect, test } from '../../fixtures/session.fixture'

test.describe('smoke: page shell', () => {
  test('dashboard renders without errors', async ({ page }) => {
    await page.goto('/dashboard')
    // TODO: replace with a selector that confirms the page is fully loaded
    await expect(page.getByRole('main')).toBeVisible()
    await expect(page.locator('.error-banner')).not.toBeVisible()
  })
})

/**
 * Screenshot capture pattern.
 *
 * Use this template when you need:
 *   1. A visual artifact for manual review (@review tag)
 *   2. A visual regression baseline (@visual tag + toHaveScreenshot)
 *
 * Run review captures:  playwright test --grep @review
 * Run visual diffs:     playwright test --grep @visual
 * Update baselines:     playwright test --grep @visual --update-snapshots
 */
import type { Locator, Page, TestInfo } from '@playwright/test'
import { expect, test } from '../../fixtures/session.fixture'

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Captures a full-page screenshot and attaches it to the test report.
 * The file lands in test-results/<test-name>/<name>.png and is visible
 * in the HTML report under the "Attachments" section.
 */
async function attachScreenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
): Promise<void> {
  const screenshotPath = testInfo.outputPath(`${name}.png`)
  await page.screenshot({ path: screenshotPath, fullPage: true, animations: 'disabled' })
  await testInfo.attach(name, { path: screenshotPath, contentType: 'image/png' })
}

/**
 * Returns locators whose content changes between runs (timestamps, counters,
 * user-specific text) so they can be masked during visual diffing.
 */
function buildDynamicMaskList(page: Page): Locator[] {
  return [
    // TODO: add locators for elements that change between runs, e.g.:
    // page.locator('.timestamp'),
    // page.locator('.user-greeting'),
  ]
}

// ── Tests ─────────────────────────────────────────────────────────────────────

test.describe('dashboard page', () => {
  test('captures a screenshot for manual review @review', async ({ page }, testInfo) => {
    await page.goto('/dashboard')
    await expect(page.getByRole('main')).toBeVisible()
    await attachScreenshot(page, testInfo, 'dashboard-page')
  })

  test('page shell is visually stable @visual', async ({ page }) => {
    await page.goto('/dashboard')
    await expect(page.getByRole('main')).toBeVisible()

    await expect(page).toHaveScreenshot('dashboard-shell.png', {
      animations: 'disabled',
      // Crop to a stable region if the full page has dynamic content:
      // clip: { x: 0, y: 0, width: 1440, height: 400 },
      mask: buildDynamicMaskList(page),
    })
  })
})

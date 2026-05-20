/**
 * No-auth test pattern.
 *
 * Use this template for tests that target a surface that does NOT require
 * a logged-in session — e.g.:
 *   - A public-facing UI
 *   - An internal proxy that forwards requests without auth
 *   - A health / status endpoint
 *
 * Naming convention: *.no-auth.spec.ts
 * These files are picked up by the 'no-auth' project in playwright.config.ts
 * and are excluded from the 'chromium' project (which requires auth.setup.ts).
 *
 * Run with:
 *   playwright test --project=no-auth
 *
 * Point at a remote env (no local stack needed):
 *   PLAYWRIGHT_SKIP_STACK_BOOT=1 \
 *   PLAYWRIGHT_BASE_URL=https://your-staging-url \
 *   PLAYWRIGHT_HEALTH_URL=https://your-staging-url \
 *   playwright test --project=no-auth
 */
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

const currentDirectoryPath = dirname(fileURLToPath(import.meta.url))
// Adjust the path to whatever fixture file your test needs:
const fixtureFilePath = resolve(currentDirectoryPath, '..', '..', '..', 'path', 'to', 'fixture.pdf')

test.describe('public surface smoke test', () => {
  test('page loads and shows expected content', async ({ page }) => {
    // Navigate using an absolute URL or a path relative to PLAYWRIGHT_BASE_URL.
    await page.goto('/')

    // TODO: replace with a selector that confirms the page loaded correctly
    await expect(page.getByRole('main')).toBeVisible()
  })

  test('file upload renders a preview without errors', async ({ page }) => {
    await page.goto('/')

    // Wait for the upload trigger to be ready
    const uploadButton = page.getByRole('button', { name: /upload|browse/i })
    await expect(uploadButton).toBeVisible()

    // Set a file on the hidden <input type="file"> — bypasses the OS picker
    const fileBuffer = await readFile(fixtureFilePath)
    await page.locator('input[type="file"]').setInputFiles({
      name: 'fixture.pdf',
      mimeType: 'application/pdf',
      buffer: fileBuffer,
    })

    // Assert no error appeared
    await expect(page.locator('.error-message')).not.toBeVisible()

    // Assert the preview rendered
    await expect(page.locator('.preview-canvas')).toBeVisible()
  })

  test('captures a screenshot artifact', async ({ page }, testInfo) => {
    await page.goto('/')
    await expect(page.getByRole('main')).toBeVisible()

    const screenshotPath = testInfo.outputPath('public-page.png')
    await page.screenshot({ path: screenshotPath, fullPage: true, animations: 'disabled' })
    await testInfo.attach('public-page', { path: screenshotPath, contentType: 'image/png' })
  })
})

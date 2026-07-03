import { type Page } from '@playwright/test'

/**
 * Optional dwell helper for demo / acceptance videos.
 *
 * The no-auth project in playwright.config.ts already applies
 * `launchOptions: { slowMo: 200 }` to make every interaction human-paced.
 * Use this helper to add extra pause time on key moments (e.g. after a page
 * transition or before a final assertion) so the recorded video is easy for
 * humans to follow.
 *
 * Set `PLAYWRIGHT_DEMO_PAUSE=0` to skip these pauses in CI.
 *
 * @param page - The Playwright page under test.
 * @param milliseconds - How long to dwell. Defaults to 900 ms.
 */
export async function humanPause(page: Page, milliseconds: number = 900): Promise<void> {
  const disabled = process.env.PLAYWRIGHT_DEMO_PAUSE === '0'
  if (disabled) return
  await page.waitForTimeout(milliseconds)
}

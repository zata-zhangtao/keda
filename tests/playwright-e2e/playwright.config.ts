import { defineConfig, devices } from '@playwright/test'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { getAdminBaseUrl, getBaseUrl } from './support/env'

const baseURL = getBaseUrl()
const adminBaseURL = getAdminBaseUrl()
const currentDirectoryPath = dirname(fileURLToPath(import.meta.url))
const authStorageStatePath = resolve(currentDirectoryPath, '.auth', 'session.json')
const adminAuthStorageStatePath = resolve(
  currentDirectoryPath,
  '.auth',
  'admin-session.json'
)
const testResultOutputDirectory = process.env.PLAYWRIGHT_TEST_RESULTS_DIR ?? './test-results'
const htmlReportOutputDirectory = process.env.PLAYWRIGHT_HTML_OUTPUT_DIR ?? 'playwright-report'
const junitOutputFilePath =
  process.env.PLAYWRIGHT_JUNIT_OUTPUT_FILE ?? `${testResultOutputDirectory}/junit.xml`

export default defineConfig({
  testDir: './tests',
  timeout: 90_000,
  fullyParallel: false,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  expect: {
    timeout: 10_000,
    toHaveScreenshot: {
      animations: 'disabled',
      caret: 'hide',
      scale: 'css',
    },
  },
  outputDir: testResultOutputDirectory,
  reporter: [
    ['list'],
    ['html', { open: 'never', outputFolder: htmlReportOutputDirectory }],
    ['junit', { outputFile: junitOutputFilePath }],
  ],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'on',
    viewport: { width: 1440, height: 1200 },
    testIdAttribute: 'data-testid',
  },
  globalSetup: './scripts/global-setup.mjs',
  globalTeardown: './scripts/global-teardown.mjs',
  projects: [
    // ── Project 1: public auth setup ───────────────────────────────────────
    // Logs into the public frontend once, persists cookies to .auth/session.json.
    {
      name: 'setup',
      testMatch: /\/auth\.setup\.ts$/,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 1200 },
      },
    },

    // ── Project 2: authenticated public tests ──────────────────────────────
    {
      name: 'chromium',
      dependencies: ['setup'],
      testIgnore: [/.*\.setup\.ts/, /.*\.no-auth\.spec\.ts/, /.*\.admin\.spec\.ts/],
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 1200 },
        storageState: authStorageStatePath,
      },
    },

    // ── Project 3: no-auth tests ───────────────────────────────────────────
    // Tests that target public/unauthenticated surfaces.
    // slowMo makes interactions human-paced, which is useful when these tests
    // are also used to record demo / acceptance videos. Pair with a local
    // `humanPause(page, ms)` helper inside tests for extra dwell time on key
    // steps (e.g. after a form fill or before an assertion).
    {
      name: 'no-auth',
      testMatch: /.*\.no-auth\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 1200 },
        launchOptions: { slowMo: 200 },
      },
    },

    // ── Project 4: admin auth setup ────────────────────────────────────────
    // Logs into the admin frontend (separate auth domain) once, persists
    // cookies to .auth/admin-session.json.
    {
      name: 'admin-setup',
      testMatch: /admin-auth\.setup\.ts$/,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 1200 },
        baseURL: adminBaseURL,
      },
    },

    // ── Project 5: authenticated admin tests ───────────────────────────────
    {
      name: 'admin',
      dependencies: ['admin-setup'],
      testMatch: /.*\.admin\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 1200 },
        baseURL: adminBaseURL,
        storageState: adminAuthStorageStatePath,
      },
    },
  ],
})

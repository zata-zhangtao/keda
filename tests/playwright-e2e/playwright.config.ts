import { defineConfig, devices } from '@playwright/test'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { getBaseUrl } from './support/env'

const baseURL = getBaseUrl()
const currentDirectoryPath = dirname(fileURLToPath(import.meta.url))
const authStorageStatePath = resolve(currentDirectoryPath, '.auth', 'session.json')
const testResultOutputDirectory = process.env.PLAYWRIGHT_TEST_RESULTS_DIR ?? './test-results'
const htmlReportOutputDirectory = process.env.PLAYWRIGHT_HTML_OUTPUT_DIR ?? 'playwright-report'
const junitOutputFilePath =
  process.env.PLAYWRIGHT_JUNIT_OUTPUT_FILE ?? 'test-results/junit.xml'

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
    video: 'retain-on-failure',
    viewport: { width: 1440, height: 1200 },
    testIdAttribute: 'data-testid',
  },
  globalSetup: './scripts/global-setup.mjs',
  globalTeardown: './scripts/global-teardown.mjs',
  projects: [
    // ── Project 1: auth setup ──────────────────────────────────────────────
    // Runs auth.setup.ts once, persists session cookies to .auth/session.json.
    {
      name: 'setup',
      testMatch: /.*\.setup\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 1200 },
      },
    },

    // ── Project 2: authenticated tests ────────────────────────────────────
    // All tests that need a logged-in session. Depends on 'setup'.
    {
      name: 'chromium',
      dependencies: ['setup'],
      testIgnore: [/.*\.setup\.ts/, /.*\.no-auth\.spec\.ts/],
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 1200 },
        storageState: authStorageStatePath,
      },
    },

    // ── Project 3: no-auth tests ───────────────────────────────────────────
    // Tests that target public/unauthenticated surfaces (e.g. a public UI,
    // a backend proxy that has no login gate).
    // Run with: playwright test --project=no-auth
    {
      name: 'no-auth',
      testMatch: /.*\.no-auth\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 1200 },
      },
    },
  ],
})

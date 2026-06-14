/**
 * Realistic validation tests for the roadmap page.
 *
 * These tests exercise the full frontend path against the real backend, but
 * route the roadmap read endpoint to deterministic data so the suite is stable
 * in CI. The data shape mirrors the real backend response and the repository
 * contains real test PRDs (P0-TEST-20260614-roadmap-e2e-*) linked to real
 * GitHub issues (#78 review, #73 merged) for manual/sandbox validation.
 * Evidence is saved to `.iar/evidence/`.
 */

import { mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '../../fixtures/session.fixture'

const currentDirectoryPath = dirname(fileURLToPath(import.meta.url))
const repositoryRootPath = resolve(currentDirectoryPath, '../../../..')
const evidenceDirectoryPath = resolve(repositoryRootPath, '.iar/evidence')

async function ensureEvidenceDirectory(): Promise<void> {
  await mkdir(evidenceDirectoryPath, { recursive: true })
}

async function saveJsonEvidence(filename: string, payload: unknown): Promise<void> {
  await ensureEvidenceDirectory()
  const filePath = resolve(evidenceDirectoryPath, filename)
  await writeFile(filePath, JSON.stringify(payload, null, 2) + '\n', 'utf-8')
}

async function saveScreenshot(page: import('@playwright/test').Page, filename: string): Promise<void> {
  await ensureEvidenceDirectory()
  const filePath = resolve(evidenceDirectoryPath, filename)
  await page.screenshot({ path: filePath, fullPage: true })
}

async function waitForPrdCards(page: import('@playwright/test').Page): Promise<void> {
  await page.waitForSelector('[data-slot="card"]', { timeout: 15_000 })
}

const MOCK_PRDS_RESPONSE = {
  prds: [
    {
      prd_path: 'tasks/pending/P0-TEST-20260614-roadmap-e2e-review.md',
      title: 'Roadmap E2E Review Highlight Test',
      status: 'pending',
      priority: 'P0',
      issue_url: 'https://github.com/zata-zhangtao/keda/issues/78',
      issue_number: 78,
      state: 'review',
      acceptance_total: 1,
      acceptance_checked: 1,
      delivery_dependencies: [],
      updated_at: '2026-06-14T00:00:00+00:00',
      block_reason: null,
      next_action: { label: '去审阅 PR', url: 'https://github.com/zata-zhangtao/keda/pull/80' },
    },
    {
      prd_path: 'tasks/pending/P0-TEST-20260614-roadmap-e2e-merged.md',
      title: 'Roadmap E2E Merged Highlight Test',
      status: 'pending',
      priority: 'P0',
      issue_url: 'https://github.com/zata-zhangtao/keda/issues/73',
      issue_number: 73,
      state: 'merged',
      acceptance_total: 1,
      acceptance_checked: 1,
      delivery_dependencies: [],
      updated_at: '2026-06-14T00:00:00+00:00',
      block_reason: null,
      next_action: { label: '开始下一个', url: null },
    },
    {
      prd_path: 'tasks/pending/P1-FEAT-20260612120000-prd-iar-init-check-gate.md',
      title: 'iar 命令仓库初始化门禁',
      status: 'pending',
      priority: 'P1',
      issue_url: null,
      issue_number: null,
      state: 'not_started',
      acceptance_total: 0,
      acceptance_checked: 0,
      delivery_dependencies: [],
      updated_at: '2026-06-12T09:50:32+00:00',
      block_reason: null,
      next_action: { label: '开始', url: null },
    },
    {
      prd_path: 'tasks/archive/P1-FEAT-20260610-114529-issue-dependency-gate.md',
      title: 'Issue Dependency Gate',
      status: 'archived',
      priority: 'P1',
      issue_url: null,
      issue_number: null,
      state: 'archived',
      acceptance_total: 1,
      acceptance_checked: 1,
      delivery_dependencies: [],
      updated_at: '2026-06-10T00:00:00+00:00',
      block_reason: null,
      next_action: null,
    },
  ],
  repo_id: 'keda-main',
  include_archived: true,
  scanned_at: '2026-06-14T00:00:00+00:00',
}

test.describe('realistic: roadmap page', () => {
  test.beforeEach(async ({ page, api }) => {
    await page.route('/api/v1/agent-runner/roadmap/prds*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_PRDS_RESPONSE),
      })
    })

    await page.route('/api/v1/agent-runner/roadmap/settings*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          repo_id: 'keda-main',
          max_parallel: 2,
          default_view: 'list',
          updated_at: '2026-06-14T00:00:00+00:00',
        }),
      })
    })

    // Keep the authenticated API client warm so the fixture stays alive.
    await api.get('/api/auth/me')
  })

  test('E2E-1 roadmap renders pending PRDs and archived switch works', async ({ page }) => {
    await page.goto('/roadmap')
    await expect(page.getByRole('heading', { name: '路线图' })).toBeVisible()
    await waitForPrdCards(page)

    await saveJsonEvidence('roadmap-prds-response.json', MOCK_PRDS_RESPONSE)
    await saveScreenshot(page, 'roadmap-list.png')

    await expect(page.getByText('Roadmap E2E Review Highlight Test')).toBeVisible()
    await expect(page.getByText('iar 命令仓库初始化门禁')).toBeVisible()
    await expect(page.getByText('Issue Dependency Gate')).not.toBeVisible()

    await page.getByLabel('显示已归档').check()
    await waitForPrdCards(page)
    await expect(page.getByText('Issue Dependency Gate')).toBeVisible()
  })

  test('E2E-1 list sorting and timeline view', async ({ page }) => {
    await page.goto('/roadmap')
    await expect(page.getByRole('heading', { name: '路线图' })).toBeVisible()
    await waitForPrdCards(page)

    await page.getByTestId('roadmap-sort-trigger').click()
    await page.getByRole('menuitemradio', { name: '按状态' }).click()
    await page.waitForTimeout(300)
    await saveScreenshot(page, 'roadmap-list-sorted.png')

    await page.getByRole('button', { name: /视图：/ }).click()
    await page.getByRole('menuitemradio', { name: '时间轴' }).click()
    await waitForPrdCards(page)
    await expect(page.getByText(/^阶段 /)).toBeVisible()
    await saveScreenshot(page, 'roadmap-timeline.png')
  })

  test('E2E-5 review and merged highlight cards', async ({ page }) => {
    await page.goto('/roadmap')
    await expect(page.getByRole('heading', { name: '路线图' })).toBeVisible()
    await waitForPrdCards(page)

    await expect(page.getByText('Roadmap E2E Review Highlight Test')).toBeVisible()
    await expect(page.getByRole('link', { name: '去审阅 PR' })).toBeVisible()
    await saveScreenshot(page, 'roadmap-review-highlight.png')

    await expect(page.getByText('Roadmap E2E Merged Highlight Test')).toBeVisible()
    await expect(page.getByText('开始下一个')).toBeVisible()
    await saveScreenshot(page, 'roadmap-merged-highlight.png')
  })
})

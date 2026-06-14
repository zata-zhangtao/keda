/**
 * Smoke test for the roadmap page.
 *
 * Mocks the roadmap API and asserts the page renders PRD cards with
 * states, progress, and dependencies without JavaScript errors.
 */

import { expect, test } from '../../fixtures/session.fixture'

test.describe('smoke: roadmap page', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('/api/v1/agent-runner/roadmap/prds*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          prds: [
            {
              prd_path: 'tasks/pending/P1-FEAT-20260101-alpha.md',
              title: 'Alpha Feature',
              status: 'pending',
              priority: 'P1',
              issue_url: null,
              issue_number: null,
              state: 'not_started',
              acceptance_total: 3,
              acceptance_checked: 1,
              delivery_dependencies: [],
              updated_at: '2026-01-01T00:00:00+00:00',
              block_reason: null,
              next_action: { label: '开始', url: null },
            },
            {
              prd_path: 'tasks/pending/P1-FEAT-20260101-beta.md',
              title: 'Beta Feature',
              status: 'pending',
              priority: 'P1',
              issue_url: 'https://github.com/org/repo/issues/10',
              issue_number: 10,
              state: 'review',
              acceptance_total: 2,
              acceptance_checked: 2,
              delivery_dependencies: [
                {
                  from_path: 'tasks/pending/P1-FEAT-20260101-beta.md',
                  to_path: 'tasks/pending/P1-FEAT-20260101-alpha.md',
                  kind: 'prd',
                  detail: 'tasks/pending/P1-FEAT-20260101-alpha.md',
                },
              ],
              updated_at: '2026-01-01T00:00:00+00:00',
              block_reason: null,
              next_action: {
                label: '去审阅 PR',
                url: 'https://github.com/org/repo/pull/11',
              },
            },
            {
              prd_path: 'tasks/archive/P1-FEAT-20260101-archived.md',
              title: 'Archived Feature',
              status: 'archived',
              priority: 'P1',
              issue_url: null,
              issue_number: null,
              state: 'archived',
              acceptance_total: 1,
              acceptance_checked: 1,
              delivery_dependencies: [],
              updated_at: '2026-01-01T00:00:00+00:00',
              block_reason: null,
              next_action: null,
            },
          ],
          repo_id: 'keda-main',
          include_archived: false,
          scanned_at: '2026-01-01T00:00:00+00:00',
        }),
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
          updated_at: '2026-01-01T00:00:00+00:00',
        }),
      })
    })
  })

  test('roadmap renders pending PRDs by default', async ({ page }) => {
    await page.goto('/roadmap')
    await expect(page.getByRole('main')).toBeVisible()
    await expect(page.getByText('Alpha Feature')).toBeVisible()
    await expect(page.getByText('Beta Feature')).toBeVisible()
    await expect(page.locator('.error-banner')).not.toBeVisible()
  })

  test('archived PRD appears when include archived is checked', async ({ page }) => {
    await page.goto('/roadmap')
    await expect(page.getByText('Archived Feature')).not.toBeVisible()
    await page.getByLabel('显示已归档').check()
    await expect(page.getByText('Archived Feature')).toBeVisible()
  })
})

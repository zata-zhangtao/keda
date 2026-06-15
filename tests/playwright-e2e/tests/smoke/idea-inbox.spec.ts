/**
 * Smoke test for the Idea Inbox page.
 *
 * Mocks the idea-inbox API endpoints to confirm the page renders
 * entries, summary, and PRD drafts, and that the append-idea form
 * posts a payload to the backend.
 */

import { expect, test } from '../../fixtures/session.fixture'

test.describe('smoke: idea inbox page', () => {
  test.beforeEach(async ({ page }) => {
    // Mock the repository snapshot endpoint.
    await page.route(
      '/api/v1/agent-runner/idea-inbox/repositories/*',
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            repo_id: 'keda-main',
            ideas_path: 'tasks/inbox/ideas.md',
            summary_path: 'tasks/inbox/summary.md',
            drafts_dir: 'tasks/inbox/prd-drafts',
            ideas_raw:
              '# Idea Inbox — 原话日志\n\n> 顶部说明。\n\n## 2026-06-15 09:00 · manual · alice (idea-1)\n\n> 第一条想法\n',
            summary_raw:
              '# Idea Inbox — AI 总结\n\n> 事实来源是 tasks/inbox/ideas.md\n\n当前收录 1 条想法。\n',
            entries: [
              {
                entry_id: 'idea-1',
                occurred_at: '2026-06-15 09:00',
                source: 'manual',
                author: 'alice',
                text: '第一条想法',
              },
            ],
            drafts: [
              {
                metadata: {
                  draft_id: '20260615-090000',
                  status: 'pending-review',
                  repo_id: 'keda-main',
                  source_idea_refs: ['idea-1'],
                  priority: 'P1',
                  prd_type: 'FEAT',
                  created_at: '2026-06-15T09:00:00+00:00',
                  approved_pending_path: null,
                },
                draft_path: 'tasks/inbox/prd-drafts/20260615-090000-test.md',
                title: '测试草稿',
                body_excerpt: '本草稿由 append-only 想法生成。',
              },
            ],
          }),
        })
      },
    )

    await page.route(
      '/api/v1/agent-runner/idea-inbox/metadata',
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            priorities: ['P0', 'P1', 'P2', 'P3'],
            prd_types: ['FEAT', 'BUG', 'CHORE'],
            inbound_signature_header: 'X-IAR-Signature',
            inbound_secret_env: 'IAR_IDEA_INBOX_INBOUND_SECRET',
          }),
        })
      },
    )

    // Mock the registry repositories endpoint to provide one enabled repo.
    await page.route('/api/v1/agent-runner/repositories*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          repositories: [
            {
              repo_id: 'keda-main',
              path: '/tmp/fake-repo',
              enabled: true,
              display_name: 'Keda Main',
              path_exists: true,
            },
          ],
        }),
      })
    })
  })

  test('idea inbox renders entries, summary, and drafts', async ({ page }) => {
    await page.goto('/ideas')
    await expect(page.getByText('Idea Inbox')).toBeVisible()
    // Entry shows up.
    await expect(page.getByText('第一条想法')).toBeVisible()
    // Draft shows up with metadata.
    await expect(page.getByText('测试草稿')).toBeVisible()
    await expect(page.getByText('pending-review')).toBeVisible()
  })

  test('append-idea form posts to backend', async ({ page }) => {
    let capturedPayload: unknown = null
    await page.route(
      '/api/v1/agent-runner/idea-inbox/repositories/*/ideas',
      async (route, request) => {
        if (request.method() === 'POST') {
          capturedPayload = JSON.parse(request.postData() ?? '{}')
          await route.fulfill({
            status: 201,
            contentType: 'application/json',
            body: JSON.stringify({
              entry: {
                entry_id: 'idea-2',
                occurred_at: '2026-06-15 10:00',
                source: 'frontend',
                author: 'operator',
                text: capturedPayload?.text ?? '',
              },
              ideas_path: 'tasks/inbox/ideas.md',
            }),
          })
          return
        }
        await route.continue()
      },
    )
    await page.goto('/ideas')
    const textarea = page.getByLabel('新想法')
    await textarea.fill('新想法原文')
    await page.getByRole('button', { name: '追加到 inbox' }).click()
    expect(capturedPayload).toMatchObject({ text: '新想法原文' })
  })
})

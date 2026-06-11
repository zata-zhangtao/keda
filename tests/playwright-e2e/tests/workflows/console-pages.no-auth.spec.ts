/**
 * 管理终端四页 smoke（mock console API，不依赖真实 GitHub / runner）。
 *
 * 所有 /api 请求都被 page.route 拦截并返回固定 fixture，
 * 验证页面渲染、操作确认框与审计列表展示。
 *
 * Run with:
 *   playwright test --project=no-auth console-pages
 */
import { expect, test, type Page } from '@playwright/test'

const LOCAL_SESSION = {
  user_id: 'local-operator',
  display_name: 'tester',
  email: 'tester@localhost',
}

const MONITORING_OVERVIEW = {
  scanned_at: '2026-06-11T10:00:00+00:00',
  unreachable_repositories: [
    {
      repo_id: 'ghost',
      display_name: 'Ghost Repo',
      configured_path: '/missing/path',
      error: "Path '/missing/path' does not exist.",
    },
  ],
  repositories: [
    {
      repo_id: 'keda-main',
      display_name: 'Keda Main',
      enabled: true,
      base_branch: 'main',
      remote: 'zata',
      health: {
        gh_available: true,
        repo_path_exists: true,
        publish_remote_exists: true,
      },
      queue_counts: {
        ready: 0,
        running: 0,
        supervising: 0,
        review: 0,
        failed: 1,
        blocked: 0,
      },
      labels: {
        ready: 'agent/ready',
        running: 'agent/running',
        supervising: 'agent/supervising',
        review: 'agent/review',
        failed: 'agent/failed',
        blocked: 'agent/blocked',
      },
      issues: [
        {
          number: 19,
          title: 'Broken Issue',
          url: 'https://example.test/19',
          labels: ['agent/failed'],
          state: 'OPEN',
          primary_label: 'agent/failed',
          pr: null,
          worktree: {
            exists: false,
            path: '',
            branch: '',
            head_sha: '',
            is_clean: true,
            dirty_files: [],
          },
          timeline: [],
          latest_event: null,
          anomalies: [],
          suggested_cli_commands: [],
          has_anomaly: false,
          anomaly_types: [],
        },
      ],
      anomaly_count: 0,
      anomaly_summary: { warning: 0, error: 0 },
      scanned_at: '2026-06-11T10:00:00+00:00',
    },
  ],
}

const COMPLETION_STATS = {
  repositories: [
    {
      repo_id: 'keda-main',
      display_name: 'Keda Main',
      total_tracked: 10,
      completed: 7,
      failed: 2,
      blocked: 1,
      open_in_pipeline: 0,
      completion_rate: 0.7,
      truncated: false,
      error: null,
    },
  ],
}

const PROCESSES = {
  processes: [
    {
      process_id: 'abc123',
      repo_id: 'keda-main',
      kind: 'daemon',
      pid: 4242,
      status: 'running',
      exit_code: null,
      log_path: '/tmp/log',
      command: ['uv', 'run', 'iar', 'daemon', '--repo-id', 'keda-main'],
      started_at: '2026-06-11T10:00:00+00:00',
      stopped_at: null,
    },
  ],
}

const REGISTRY = {
  repositories: [
    {
      repo_id: 'keda-main',
      path: '/Users/me/code/keda',
      enabled: true,
      display_name: 'Keda Main',
      path_exists: true,
    },
  ],
}

const AUDITS = {
  audits: [
    {
      occurred_at: '2026-06-11T10:30:00+00:00',
      actor: 'console',
      action: 'retry_failed',
      repo_id: 'keda-main',
      issue_number: 19,
      params_json: '{}',
      result: 'accepted',
      detail: "Issue #19: 'agent/failed' -> 'agent/ready'.",
    },
  ],
}

async function mockConsoleApi(page: Page): Promise<void> {
  await page.route('**/api/auth/me', (route) =>
    route.fulfill({ json: LOCAL_SESSION }),
  )
  await page.route('**/api/v1/agent-runner/overview', (route) =>
    route.fulfill({ json: MONITORING_OVERVIEW }),
  )
  await page.route('**/api/v1/agent-runner/console/stats/overview', (route) =>
    route.fulfill({ json: COMPLETION_STATS }),
  )
  await page.route('**/api/v1/agent-runner/console/stats/history**', (route) =>
    route.fulfill({
      json: { repo_id: null, days: 30, trend: [] },
    }),
  )
  await page.route('**/api/v1/agent-runner/console/runs**', (route) =>
    route.fulfill({ json: { runs: [] } }),
  )
  await page.route('**/api/v1/agent-runner/console/processes', (route) =>
    route.fulfill({ json: PROCESSES }),
  )
  await page.route('**/api/v1/agent-runner/repositories', (route) =>
    route.fulfill({ json: REGISTRY }),
  )
  await page.route('**/api/v1/agent-runner/console/audit**', (route) =>
    route.fulfill({ json: AUDITS }),
  )
}

test.describe('console pages smoke (mocked API)', () => {
  test('dashboard shows queue, completion summary and unreachable warning', async ({
    page,
  }) => {
    await mockConsoleApi(page)
    await page.goto('/dashboard')
    await expect(
      page.getByRole('heading', { name: 'Agent Runner 管理终端' }),
    ).toBeVisible()
    await expect(page.getByText('完成率 70%')).toBeVisible()
    await expect(page.getByText('个已注册仓库无法访问')).toBeVisible()
    // failed Issue 选中后出现「重试」操作条。
    await expect(page.getByRole('button', { name: '重试' })).toBeVisible()
  })

  test('retry action asks for confirmation and posts whitelisted action', async ({
    page,
  }) => {
    await mockConsoleApi(page)
    let actionRequestBody: unknown = null
    await page.route(
      '**/api/v1/agent-runner/console/repositories/keda-main/issues/19/actions',
      (route) => {
        actionRequestBody = route.request().postDataJSON()
        return route.fulfill({
          json: {
            action: 'retry_failed',
            result: 'accepted',
            detail: "Issue #19: 'agent/failed' -> 'agent/ready'.",
            process: null,
          },
        })
      },
    )

    // 第一次 dismiss 确认框 → 不应发请求。
    page.once('dialog', (dialog) => void dialog.dismiss())
    await page.goto('/dashboard')
    await page.getByRole('button', { name: '重试' }).click()
    expect(actionRequestBody).toBeNull()

    // 第二次 accept 确认框 → 发送白名单动作。
    page.once('dialog', (dialog) => void dialog.accept())
    await page.getByRole('button', { name: '重试' }).click()
    await expect
      .poll(() => actionRequestBody, { timeout: 5_000 })
      .toEqual({ action: 'retry_failed' })
  })

  test('processes page lists managed processes', async ({ page }) => {
    await mockConsoleApi(page)
    await page.goto('/processes')
    await expect(page.getByRole('heading', { name: '托管进程' })).toBeVisible()
    await expect(page.getByText('4242')).toBeVisible()
    await expect(page.getByRole('button', { name: '停止' })).toBeVisible()
  })

  test('stats page shows completion table', async ({ page }) => {
    await mockConsoleApi(page)
    await page.goto('/stats')
    await expect(page.getByRole('heading', { name: '完成度统计' })).toBeVisible()
    await expect(page.getByText('70%')).toBeVisible()
  })

  test('repositories page shows registry and audit log', async ({ page }) => {
    await mockConsoleApi(page)
    await page.goto('/repositories')
    await expect(page.getByRole('heading', { name: '项目接入' })).toBeVisible()
    await expect(page.getByText('/Users/me/code/keda')).toBeVisible()
    await expect(page.getByText('retry_failed')).toBeVisible()
  })
})

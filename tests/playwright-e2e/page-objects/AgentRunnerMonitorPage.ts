/**
 * Page object for the Agent Runner monitoring dashboard.
 *
 * Renders `/dashboard` and surfaces selectors and convenience helpers used by
 * Playwright smoke and workflow tests.
 */

import { expect, type Page } from '@playwright/test'

const OVERVIEW_ENDPOINT = /\/api\/v1\/agent-runner\/overview$/
const ISSUE_DETAIL_ENDPOINT = /\/api\/v1\/agent-runner\/issues\/\d+$/

export type MonitoringOverview = {
  repositories: Array<{
    repo_id: string
    display_name: string
    enabled: boolean
    base_branch: string
    remote: string
    health: {
      gh_available: boolean
      repo_path_exists: boolean
      publish_remote_exists: boolean
    }
    queue_counts: Record<string, number>
    labels: Record<string, string>
    issues: Array<{
      number: number
      title: string
      url: string
      labels: string[]
      state: string
      primary_label: string
      pr: {
        number: number | null
        url: string
        branch: string
        head_sha: string
        base_sha: string
        mergeable: boolean | null
        checks_state: string | null
        checks_summary: string[]
      } | null
      worktree: {
        exists: boolean
        path: string
        branch: string
        head_sha: string
        is_clean: boolean
        dirty_files: string[]
      }
      timeline: Array<{
        phase: string
        cycle: number
        comment_index: number
        action: string | null
        head_sha: string | null
        pr_branch: string | null
        checks_state: string | null
        mergeable: boolean | null
        raw_marker: string
      }>
      latest_event: {
        version: number
        phase: string
        cycle: number
        head_sha: string | null
        base_sha: string | null
        pr_branch: string | null
        action: string | null
        checks_state: string | null
        mergeable: boolean | null
        issue_comments_count: number | null
        pr_comments_count: number | null
      } | null
      anomalies: Array<{
        type: string
        severity: string
        message: string
        suggested_cli: string[]
      }>
      suggested_cli_commands: string[]
      has_anomaly: boolean
      anomaly_types: string[]
    }>
    anomaly_count: number
    anomaly_summary: { warning: number; error: number }
    scanned_at: string
  }>
  scanned_at: string
}

export class AgentRunnerMonitorPage {
  constructor(public readonly page: Page) {}

  /**
   * Stub the dashboard's read-only backend with deterministic JSON. The panel
   * must work end-to-end without touching real GitHub.
   */
  async mockOverview(overview: MonitoringOverview): Promise<void> {
    await this.page.route(OVERVIEW_ENDPOINT, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(overview),
      })
    })
  }

  async mockIssueDetail(
    issueNumber: number,
    detail: MonitoringOverview['repositories'][number]['issues'][number],
  ): Promise<void> {
    await this.page.route(ISSUE_DETAIL_ENDPOINT, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(detail),
      })
    })
  }

  async goto(): Promise<void> {
    await this.page.goto('/dashboard')
  }

  async expectHeading(): Promise<void> {
    await expect(
      this.page.getByRole('heading', { name: 'Agent Runner Monitor' }),
    ).toBeVisible()
  }

  async expectRepositoryVisible(displayName: string): Promise<void> {
    await expect(
      this.page.getByText(displayName, { exact: false }).first(),
    ).toBeVisible()
  }

  async expectAnomalyCount(count: number): Promise<void> {
    await expect(
      this.page.getByText(new RegExp(`异常\\s+${count}\\b`)).first(),
    ).toBeVisible()
  }

  async openIssue(issueNumber: number): Promise<void> {
    await this.page
      .getByRole('button', { name: new RegExp(`#${issueNumber}\\b`) })
      .first()
      .click()
  }

  async expectAnomalyCard(anomalyType: string): Promise<void> {
    await expect(this.page.getByText(anomalyType, { exact: true })).toBeVisible()
  }

  async expectSuggestedCommand(command: string): Promise<void> {
    await expect(this.page.getByText(command, { exact: true })).toBeVisible()
  }
}

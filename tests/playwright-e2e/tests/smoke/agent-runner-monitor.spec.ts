/**
 * Smoke test for the Agent Runner monitoring dashboard.
 *
 * Validates PRD scenarios 9 and 10 without touching real GitHub. We stub the
 * read-only `/api/v1/agent-runner/*` endpoints and assert the dashboard
 * renders the queue summary, anomaly indicators, event timeline, and
 * copyable suggested CLI commands.
 */

import { test } from '../../fixtures/session.fixture'
import { AgentRunnerMonitorPage } from '../../page-objects/AgentRunnerMonitorPage'
import type { MonitoringOverview } from '../../page-objects/AgentRunnerMonitorPage'

const MOCKED_OVERVIEW: MonitoringOverview = {
  repositories: [
    {
      repo_id: 'zata/keda-test',
      display_name: 'Keda Test',
      enabled: true,
      base_branch: 'main',
      remote: 'origin',
      health: {
        gh_available: true,
        repo_path_exists: true,
        publish_remote_exists: true,
      },
      queue_counts: {
        ready: 0,
        running: 0,
        supervising: 1,
        review: 0,
        failed: 0,
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
          number: 100,
          title: 'Test Issue with Anomaly',
          url: 'https://github.com/zata/keda-test/issues/100',
          labels: ['agent/supervising', 'source/prd'],
          state: 'open',
          primary_label: 'agent/supervising',
          pr: {
            number: 101,
            url: 'https://github.com/zata/keda-test/pull/101',
            branch: 'issue-100',
            head_sha: 'a1b2c3d',
            base_sha: 'b2c3d4e',
            mergeable: true,
            checks_state: 'SUCCESS',
            checks_summary: [],
          },
          worktree: {
            exists: true,
            path: '/tmp/wt-issue-100',
            branch: 'issue-100',
            head_sha: 'a1b2c3d',
            is_clean: false,
            dirty_files: ['tasks/pending/foo.md'],
          },
          timeline: [
            {
              phase: 'claimed',
              cycle: 1,
              comment_index: 0,
              action: null,
              head_sha: 'a1b2c3d',
              pr_branch: 'issue-100',
              checks_state: null,
              mergeable: null,
              raw_marker: '<!-- iar:event version=1 phase=claimed cycle=1 -->',
            },
            {
              phase: 'draft_pr_created',
              cycle: 2,
              comment_index: 1,
              action: null,
              head_sha: 'a1b2c3d',
              pr_branch: 'issue-100',
              checks_state: null,
              mergeable: null,
              raw_marker: '<!-- iar:event version=1 phase=draft_pr_created cycle=2 -->',
            },
          ],
          latest_event: {
            version: 1,
            phase: 'draft_pr_created',
            cycle: 2,
            head_sha: 'a1b2c3d',
            base_sha: null,
            pr_branch: 'issue-100',
            action: null,
            checks_state: null,
            mergeable: null,
            issue_comments_count: null,
            pr_comments_count: null,
          },
          anomalies: [
            {
              type: 'dirty_worktree_mismatch',
              severity: 'warning',
              message:
                'Worktree has uncommitted changes but Issue is not in running state.',
              suggested_cli: ['iar run-once --dry-run', 'git status'],
            },
          ],
          suggested_cli_commands: ['iar run-once --dry-run', 'git status'],
          has_anomaly: true,
          anomaly_types: ['dirty_worktree_mismatch'],
        },
      ],
      anomaly_count: 1,
      anomaly_summary: { warning: 1, error: 0 },
      scanned_at: '2026-05-24T12:00:00+00:00',
    },
  ],
  scanned_at: '2026-05-24T12:00:00+00:00',
}

test.describe('smoke: agent-runner monitor', () => {
  test('renders overview, anomaly indicator, timeline and copyable CLI', async ({
    page,
  }) => {
    const monitor = new AgentRunnerMonitorPage(page)
    await monitor.mockOverview(MOCKED_OVERVIEW)
    await monitor.goto()

    await monitor.expectHeading()
    await monitor.expectRepositoryVisible('Keda Test')
    await monitor.expectAnomalyCount(1)

    await monitor.openIssue(100)
    await monitor.expectAnomalyCard('dirty_worktree_mismatch')
    await monitor.expectSuggestedCommand('iar run-once --dry-run')
  })
})

---
id: github-trending
schedule: "0 8 * * *"
repo_id: keda
priority: P2
issue_type: feature
agent: auto
labels:
  - area/discovery
publish_prd: true
queue_ready: true
run_now: false
timezone: Asia/Shanghai
slug: github-trending
---

# PRD: Daily GitHub Trending digest

Tracked implementation task for **Daily GitHub Trending digest**.

## Goal

Every morning, scan the GitHub Trending page, summarize the top repositories
in the language of the day, and publish a digest into the project knowledge
base so engineers can quickly see what's gaining traction.

## Acceptance Checklist

- [ ] Fetch today's GitHub Trending list.
- [ ] Pick the top 10 repositories by star velocity.
- [ ] For each repository, capture name, language, description, and star delta.
- [ ] Write the digest to `docs/trending/{{date}}.md`.
- [ ] Open a draft PR with the digest change and link this Issue.

## Reference data

- Trigger date: {{date}}
- Loop id: {{loop_id}}
- Target repository: {{repo_id}}

## Delivery Notes

- Recommended branch: `task/<issue-number>-prd-github-trending-{{date}}`
- Worktree command: `just worktree --issue <issue-number>`
- PR should include: `Closes #<issue-number>`

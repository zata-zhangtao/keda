/**
 * Stack lifecycle helpers for Playwright global setup / teardown.
 *
 * Behaviour:
 *   docker mode  → runs PLAYWRIGHT_STACK_UP_COMMAND before tests,
 *                  PLAYWRIGHT_STACK_DOWN_COMMAND after (unless PLAYWRIGHT_KEEP_STACK=1).
 *   dev mode     → assumes services are already running; only polls readiness.
 *
 * Key env vars:
 *   PLAYWRIGHT_SKIP_STACK_BOOT=1   Skip boot + always use dev mode URL defaults.
 *   PLAYWRIGHT_STACK_MODE          'docker' | 'dev'
 *   PLAYWRIGHT_BASE_URL            Override the primary app URL readiness check.
 *   PLAYWRIGHT_HEALTH_URL          Override the API health-check URL.
 *   PLAYWRIGHT_STACK_UP_COMMAND    Shell command to bring the stack up (docker mode).
 *   PLAYWRIGHT_STACK_DOWN_COMMAND  Shell command to bring the stack down (docker mode).
 *   PLAYWRIGHT_STACK_TIMEOUT_MS    Readiness polling timeout in ms (default 240000).
 *   PLAYWRIGHT_KEEP_STACK=1        Skip teardown even in docker mode.
 */

import { execSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

const currentDirectoryPath = dirname(fileURLToPath(import.meta.url))
const repositoryRootPath = resolve(currentDirectoryPath, '../..')

// ── NO_PROXY ──────────────────────────────────────────────────────────────────

function appendProxyBypassList(rawBypassValue) {
  const entries = (rawBypassValue ?? '')
    .split(',')
    .map((e) => e.trim())
    .filter(Boolean)
  if (!entries.includes('127.0.0.1')) entries.push('127.0.0.1')
  if (!entries.includes('localhost')) entries.push('localhost')
  return entries.join(',')
}

process.env.NO_PROXY = appendProxyBypassList(process.env.NO_PROXY)
process.env.no_proxy = appendProxyBypassList(process.env.no_proxy)

// ── Mode helpers ──────────────────────────────────────────────────────────────

function isSkippingManagedStackBoot() {
  return process.env.PLAYWRIGHT_SKIP_STACK_BOOT === '1'
}

function resolveStackMode() {
  const configured = process.env.PLAYWRIGHT_STACK_MODE?.trim()
  if (!configured) return isSkippingManagedStackBoot() ? 'dev' : 'docker'
  if (configured === 'docker' || configured === 'dev') return configured
  throw new Error(`Unsupported PLAYWRIGHT_STACK_MODE '${configured}'. Use 'docker' or 'dev'.`)
}

// ── Defaults per mode ─────────────────────────────────────────────────────────
// TODO: set the ports / commands that match your project.

function resolveModeDefaults() {
  const stackMode = resolveStackMode()

  if (stackMode === 'docker') {
    return {
      stackUpCommand: process.env.PLAYWRIGHT_STACK_UP_COMMAND ?? 'docker compose up -d --wait',
      stackDownCommand: process.env.PLAYWRIGHT_STACK_DOWN_COMMAND ?? 'docker compose down',
      readinessUrlList: [
        process.env.PLAYWRIGHT_HEALTH_URL ?? 'http://127.0.0.1:8000/healthz',
        process.env.PLAYWRIGHT_BASE_URL  ?? 'http://127.0.0.1:8080',
      ],
    }
  }

  return {
    stackUpCommand: process.env.PLAYWRIGHT_STACK_UP_COMMAND ?? '',
    stackDownCommand: process.env.PLAYWRIGHT_STACK_DOWN_COMMAND ?? '',
    readinessUrlList: [
      process.env.PLAYWRIGHT_HEALTH_URL ?? 'http://127.0.0.1:8000/healthz',
      process.env.PLAYWRIGHT_BASE_URL  ?? 'http://127.0.0.1:5173',
    ],
  }
}

// ── Readiness polling ─────────────────────────────────────────────────────────

async function isUrlReady(url) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(5_000) })
    return response.ok
  } catch {
    return false
  }
}

async function waitForReadinessUrlList(readinessUrlList) {
  const timeoutMs = Number(process.env.PLAYWRIGHT_STACK_TIMEOUT_MS ?? 240_000)
  const pollIntervalMs = Number(process.env.PLAYWRIGHT_STACK_POLL_INTERVAL_MS ?? 2_000)
  const deadline = Date.now() + timeoutMs

  while (Date.now() < deadline) {
    const results = await Promise.all(readinessUrlList.map(isUrlReady))
    if (results.every(Boolean)) return
    await delay(pollIntervalMs)
  }

  throw new Error(
    `Timed out waiting for Playwright stack readiness: ${readinessUrlList.join(', ')}`,
  )
}

// ── Exports ───────────────────────────────────────────────────────────────────

export async function ensurePlaywrightStackReady() {
  const skipBoot = isSkippingManagedStackBoot()
  const { stackUpCommand, readinessUrlList } = resolveModeDefaults()
  const stackMode = resolveStackMode()

  if (!skipBoot) {
    if (!stackUpCommand) {
      throw new Error(
        `PLAYWRIGHT_STACK_MODE=${stackMode} requires PLAYWRIGHT_STACK_UP_COMMAND or manual boot.`,
      )
    }
    // When using the default docker compose command, verify a compose file exists in the repo root.
    if (stackMode === 'docker' && !process.env.PLAYWRIGHT_STACK_UP_COMMAND) {
      const candidateComposeFileNames = ['docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml']
      const composeFileExists = candidateComposeFileNames.some((fileName) =>
        existsSync(resolve(repositoryRootPath, fileName)),
      )
      if (!composeFileExists) {
        throw new Error(
          `Docker stack mode requires a compose file in the repository root (${repositoryRootPath}).\n` +
          `Expected one of: ${candidateComposeFileNames.join(', ')}\n` +
          `Options:\n` +
          `  • Add a docker-compose.yml to the repository root, or\n` +
          `  • Set PLAYWRIGHT_STACK_UP_COMMAND to a custom compose command, or\n` +
          `  • Set PLAYWRIGHT_SKIP_STACK_BOOT=1 to target an already-running stack.`,
        )
      }
    }
    execSync(stackUpCommand, {
      cwd: repositoryRootPath,
      env: process.env,
      shell: true,
      stdio: 'inherit',
    })
  }

  await waitForReadinessUrlList(readinessUrlList)
}

export async function teardownPlaywrightStack() {
  const skipBoot = isSkippingManagedStackBoot()
  const keepStack = process.env.PLAYWRIGHT_KEEP_STACK === '1'
  const { stackDownCommand } = resolveModeDefaults()

  if (skipBoot || keepStack || !stackDownCommand) return

  execSync(stackDownCommand, {
    cwd: repositoryRootPath,
    env: process.env,
    shell: true,
    stdio: 'inherit',
  })
}

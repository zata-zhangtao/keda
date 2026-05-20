import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// ── Types ─────────────────────────────────────────────────────────────────────

export type Credentials = {
  identifier: string
  password: string
}

/** 'docker' = Playwright manages a local Docker Compose stack.
 *  'dev'    = An external dev server is already running. */
export type StackMode = 'docker' | 'dev'

// ── Internals ─────────────────────────────────────────────────────────────────

const currentDirectoryPath = dirname(fileURLToPath(import.meta.url))
const e2eRootPath = resolve(currentDirectoryPath, '..')
const authDirectoryPath = resolve(e2eRootPath, '.auth')
const authStorageStatePath = resolve(authDirectoryPath, 'session.json')

function trimTrailingSlash(rawUrl: string): string {
  return rawUrl.replace(/\/$/, '')
}

function readFirstDefinedEnv(keyList: string[]): string | null {
  for (const key of keyList) {
    const value = process.env[key]?.trim()
    if (value) return value
  }
  return null
}

function isSkippingManagedStackBoot(): boolean {
  return process.env.PLAYWRIGHT_SKIP_STACK_BOOT === '1'
}

// ── Stack mode ────────────────────────────────────────────────────────────────

/**
 * Returns the resolved stack mode.
 * Defaults to 'dev' when PLAYWRIGHT_SKIP_STACK_BOOT=1, otherwise 'docker'.
 */
export function getStackMode(): StackMode {
  const configured = process.env.PLAYWRIGHT_STACK_MODE?.trim()
  if (!configured) return isSkippingManagedStackBoot() ? 'dev' : 'docker'
  if (configured === 'docker' || configured === 'dev') return configured
  throw new Error(`Unsupported PLAYWRIGHT_STACK_MODE '${configured}'. Use 'docker' or 'dev'.`)
}

// ── URLs ──────────────────────────────────────────────────────────────────────

/**
 * Primary application URL (used as Playwright baseURL).
 *
 * Override: PLAYWRIGHT_BASE_URL
 * Docker default:  http://127.0.0.1:PORT_A  ← fill in your docker port
 * Dev default:     http://127.0.0.1:PORT_B  ← fill in your dev server port
 */
export function getBaseUrl(): string {
  const stackMode = getStackMode()
  const fallback = stackMode === 'docker' ? 'http://127.0.0.1:8080' : 'http://127.0.0.1:5173'
  return trimTrailingSlash(process.env.PLAYWRIGHT_BASE_URL ?? fallback)
}

/**
 * API base URL used by the authenticated API seed client.
 *
 * Override: PLAYWRIGHT_API_BASE_URL
 */
export function getApiBaseUrl(): string {
  const configured = process.env.PLAYWRIGHT_API_BASE_URL?.trim()
  if (configured) return trimTrailingSlash(configured)

  const stackMode = getStackMode()
  return stackMode === 'docker' ? 'http://127.0.0.1:8000' : 'http://127.0.0.1:8000'
}

/**
 * URL checked by the global-setup readiness poller before tests start.
 *
 * Override: PLAYWRIGHT_HEALTH_URL
 * Default: same as getApiBaseUrl() + '/healthz'
 */
export function getHealthUrl(): string {
  return process.env.PLAYWRIGHT_HEALTH_URL?.trim() ?? `${getApiBaseUrl()}/healthz`
}

// ── Credentials ───────────────────────────────────────────────────────────────

/**
 * Resolves the login credentials used by the auth setup step.
 *
 * Reads from (in order):
 *   PLAYWRIGHT_IDENTIFIER  or  APP_BOOTSTRAP_USERNAME / APP_BOOTSTRAP_EMAIL
 *   PLAYWRIGHT_PASSWORD    or  APP_BOOTSTRAP_PASSWORD
 */
export function getCredentials(): Credentials {
  const identifier =
    readFirstDefinedEnv(['PLAYWRIGHT_IDENTIFIER', 'APP_BOOTSTRAP_USERNAME', 'APP_BOOTSTRAP_EMAIL']) ?? ''
  const password =
    readFirstDefinedEnv(['PLAYWRIGHT_PASSWORD', 'APP_BOOTSTRAP_PASSWORD']) ?? ''

  if (!identifier || !password) {
    throw new Error(
      'Missing Playwright credentials. Set PLAYWRIGHT_IDENTIFIER and PLAYWRIGHT_PASSWORD ' +
        '(or the APP_BOOTSTRAP_* equivalents) before running e2e tests.',
    )
  }

  return { identifier, password }
}

// ── Auth storage state ────────────────────────────────────────────────────────

/** Ensures the .auth/ directory exists (called by auth.setup.ts). */
export function ensureAuthDirectory(): void {
  mkdirSync(authDirectoryPath, { recursive: true })
}

/** Path to the persisted Playwright storage state used by the chromium project. */
export function getAuthStorageStatePath(): string {
  ensureAuthDirectory()
  return authStorageStatePath
}

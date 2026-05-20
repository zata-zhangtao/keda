import type { APIRequestContext, APIResponse } from '@playwright/test'
import { request as playwrightRequest } from '@playwright/test'
import { getApiBaseUrl, getCredentials } from './env'

// ── Types ─────────────────────────────────────────────────────────────────────
// Add your own resource record types here, mirroring the API response shapes.

// export type UserRecord = { id: string; email: string }

// ── Client ────────────────────────────────────────────────────────────────────

/**
 * Thin authenticated HTTP client for Playwright seed/cleanup helpers.
 *
 * Usage in a fixture:
 *   const api = await ApiClient.createAuthenticated()
 *   try { await use(api) } finally { await api.dispose() }
 *
 * Adapt `login()` to match your application's auth endpoint.
 */
export class ApiClient {
  private constructor(private readonly context: APIRequestContext) {}

  // ── Factory ────────────────────────────────────────────────────────────────

  static async createAuthenticated(): Promise<ApiClient> {
    const context = await playwrightRequest.newContext({
      baseURL: getApiBaseUrl(),
      extraHTTPHeaders: { Accept: 'application/json' },
    })
    const client = new ApiClient(context)
    await client.login()
    return client
  }

  async dispose(): Promise<void> {
    await this.context.dispose()
  }

  // ── Auth ───────────────────────────────────────────────────────────────────

  /**
   * Authenticates the client.
   * Adapt the endpoint path and payload shape to your application.
   */
  private async login(): Promise<void> {
    const { identifier, password } = getCredentials()
    const response = await this.context.post('/auth/login', {
      data: { identifier, password },
    })
    await this.parseResponse<unknown>(response, 'Failed to authenticate the Playwright API client.')
  }

  // ── HTTP helpers ───────────────────────────────────────────────────────────

  async get<T>(path: string): Promise<T> {
    return this.parseResponse<T>(await this.context.get(path), `GET ${path} failed.`)
  }

  async post<T>(path: string, data: Record<string, unknown>): Promise<T> {
    return this.parseResponse<T>(
      await this.context.post(path, { data }),
      `POST ${path} failed.`,
    )
  }

  async delete(path: string): Promise<void> {
    await this.parseResponse<void>(
      await this.context.delete(path),
      `DELETE ${path} failed.`,
    )
  }

  async postMultipart<T>(
    path: string,
    multipart: Record<string, unknown>,
  ): Promise<T> {
    return this.parseResponse<T>(
      await this.context.post(path, { multipart }),
      `POST (multipart) ${path} failed.`,
    )
  }

  private async parseResponse<T>(response: APIResponse, failureMessage: string): Promise<T> {
    if (!response.ok()) {
      const body = await response.text()
      throw new Error(`${failureMessage} (${response.status()}): ${body}`)
    }
    if (response.status() === 204 || response.status() === 205) return undefined as T
    return (await response.json()) as T
  }
}

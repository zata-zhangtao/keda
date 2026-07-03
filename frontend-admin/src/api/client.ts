import axios, { AxiosError, type AxiosRequestConfig } from 'axios'

export const apiClient = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
  withCredentials: true,
})

/** Error thrown for failed API requests with status and detail. */
export class ApiRequestError extends Error {
  status: number
  detail: unknown

  /**
   * Create an API request error.
   *
   * @param status - HTTP response status code.
   * @param message - Human-readable error message.
   * @param detail - Raw error detail from the server.
   */
  constructor(status: number, message: string, detail: unknown) {
    super(message)
    this.status = status
    this.detail = detail
    this.name = 'ApiRequestError'
  }
}

/** Resolve a user-facing error message from an Axios error. */
function resolveErrorMessage(error: AxiosError): string {
  const data = error.response?.data
  if (data && typeof data === 'object') {
    if ('detail' in data && typeof data.detail === 'string') return data.detail
    if ('message' in data && typeof data.message === 'string') return data.message
  }
  if (typeof data === 'string' && data.length > 0) return data
  return error.message || `HTTP ${error.response?.status}`
}

/** Throw an ``ApiRequestError`` for an Axios failure, otherwise re-throw. */
export function handleApiError(error: unknown): never {
  if (error instanceof AxiosError && error.response) {
    throw new ApiRequestError(
      error.response.status,
      resolveErrorMessage(error),
      error.response.data
    )
  }
  throw error
}

/** Send a GET request and return the typed response data. */
export async function apiGet<T>(path: string, config?: AxiosRequestConfig): Promise<T> {
  try {
    const response = await apiClient.get<T>(path, config)
    return response.data
  } catch (error) {
    handleApiError(error)
  }
}

/** Send a POST request with an optional body and return the typed response data. */
export async function apiPost<T>(
  path: string,
  body?: unknown,
  config?: AxiosRequestConfig
): Promise<T> {
  try {
    const response = await apiClient.post<T>(path, body, config)
    return response.data
  } catch (error) {
    handleApiError(error)
  }
}

/** Send a PATCH request with an optional body and return the typed response data. */
export async function apiPatch<T>(
  path: string,
  body?: unknown,
  config?: AxiosRequestConfig
): Promise<T> {
  try {
    const response = await apiClient.patch<T>(path, body, config)
    return response.data
  } catch (error) {
    handleApiError(error)
  }
}

/** Send a DELETE request and return the typed response data. */
export async function apiDelete<T>(path: string, config?: AxiosRequestConfig): Promise<T> {
  try {
    const response = await apiClient.delete<T>(path, config)
    return response.data
  } catch (error) {
    handleApiError(error)
  }
}

import axios from "axios"

/** Axios instance for the public frontend API with credentials and 401 handling. */
export const apiClient = axios.create({
  baseURL: "/api",
  headers: {
    "Content-Type": "application/json",
  },
  withCredentials: true,
})

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (axios.isAxiosError(error) && error.response?.status === 401) {
      if (typeof window !== "undefined") {
        window.location.href = "/login"
      }
    }
    return Promise.reject(error)
  }
)

/** Error thrown for API requests with an optional HTTP status code. */
export class ApiRequestError extends Error {
  /**
   * Error thrown for API requests with an optional HTTP status code.
   *
   * @param message - Human-readable error message.
   * @param status - Optional HTTP response status code.
   */
  constructor(
    message: string,
    public status?: number
  ) {
    super(message)
    this.name = "ApiRequestError"
  }
}

/** Send a GET request and return the typed response data. */
export async function apiGet<T>(url: string): Promise<T> {
  try {
    const response = await apiClient.get<T>(url)
    return response.data
  } catch (error) {
    if (axios.isAxiosError(error)) {
      const detail = error.response?.data?.detail
      throw new ApiRequestError(
        typeof detail === "string" ? detail : error.message,
        error.response?.status
      )
    }
    throw error
  }
}

/** Send a POST request with a JSON body and return the typed response data. */
export async function apiPost<T>(url: string, data: unknown): Promise<T> {
  try {
    const response = await apiClient.post<T>(url, data)
    return response.data
  } catch (error) {
    if (axios.isAxiosError(error)) {
      const detail = error.response?.data?.detail
      throw new ApiRequestError(
        typeof detail === "string" ? detail : error.message,
        error.response?.status
      )
    }
    throw error
  }
}

/** Send a PUT request with a JSON body and return the typed response data. */
export async function apiPut<T>(url: string, data: unknown): Promise<T> {
  try {
    const response = await apiClient.put<T>(url, data)
    return response.data
  } catch (error) {
    if (axios.isAxiosError(error)) {
      const detail = error.response?.data?.detail
      throw new ApiRequestError(
        typeof detail === "string" ? detail : error.message,
        error.response?.status
      )
    }
    throw error
  }
}

/** Send a DELETE request and throw on failure. */
export async function apiDelete(url: string): Promise<void> {
  try {
    await apiClient.delete(url)
  } catch (error) {
    if (axios.isAxiosError(error)) {
      const detail = error.response?.data?.detail
      throw new ApiRequestError(
        typeof detail === "string" ? detail : error.message,
        error.response?.status
      )
    }
    throw error
  }
}

/** Send a PATCH request with a JSON body and return the typed response data. */
export async function apiPatch<T>(url: string, data: unknown): Promise<T> {
  try {
    const response = await apiClient.patch<T>(url, data)
    return response.data
  } catch (error) {
    if (axios.isAxiosError(error)) {
      const detail = error.response?.data?.detail
      throw new ApiRequestError(
        typeof detail === "string" ? detail : error.message,
        error.response?.status
      )
    }
    throw error
  }
}

// keda/frontend 的 agentRunner/console/ideaInbox/roadmap 模块沿用 fetch 风格的
// 动词命名（get/post/put/patch/del）。以下别名让这些吸收进来的模块无需逐个
// 改写调用点，与本包 axios 风格的 apiGet/apiPost/apiPut/apiPatch/apiDelete 共存。
export const get = apiGet
export const put = apiPut
export const patch = apiPatch

/** post 别名：data 可省略（与 keda fetch 客户端签名对齐）。 */
export async function post<T>(url: string, data?: unknown): Promise<T> {
  return apiPost<T>(url, data ?? {})
}

/** del 别名：keda 模块按 Promise<T> 声明，这里保留同样的返回类型。 */
export async function del<T>(url: string): Promise<T> {
  await apiDelete(url)
  return undefined as T
}

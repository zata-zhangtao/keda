const API_BASE = "/api";

type ApiRequestOptions = {
  headers?: HeadersInit;
  retryOnUnauthorized?: boolean;
};

export interface ApiError {
  detail: unknown;
}

export type ApiErrorDetail = string | Record<string, unknown> | unknown[] | null;

export class ApiRequestError extends Error {
  status: number;
  detail: ApiErrorDetail;

  constructor(params: { status: number; message: string; detail: ApiErrorDetail }) {
    super(params.message);
    this.name = "ApiRequestError";
    this.status = params.status;
    this.detail = params.detail;
  }
}

function resolveApiErrorDetail(errorPayload: unknown): ApiErrorDetail {
  if (typeof errorPayload === "string" || Array.isArray(errorPayload)) {
    return errorPayload;
  }
  if (!errorPayload || typeof errorPayload !== "object") {
    return null;
  }
  if ("detail" in errorPayload) {
    const detailValue = (errorPayload as ApiError).detail;
    if (
      typeof detailValue === "string" ||
      Array.isArray(detailValue) ||
      (detailValue && typeof detailValue === "object")
    ) {
      return detailValue as ApiErrorDetail;
    }
  }
  return errorPayload as Record<string, unknown>;
}

function resolveApiErrorMessage(statusCode: number, errorDetail: ApiErrorDetail): string {
  if (typeof errorDetail === "string" && errorDetail.trim().length > 0) {
    return errorDetail;
  }
  if (errorDetail && typeof errorDetail === "object" && !Array.isArray(errorDetail)) {
    const detailMessage = (errorDetail as Record<string, unknown>).message;
    if (typeof detailMessage === "string" && detailMessage.trim().length > 0) {
      return detailMessage;
    }
  }
  return `HTTP ${statusCode}`;
}

async function buildApiRequestError(response: Response): Promise<ApiRequestError> {
  const errorResponseText = await response.text().catch(() => "");
  let errorPayload: unknown = { detail: "Unknown error" };
  if (errorResponseText.trim().length > 0) {
    try {
      errorPayload = JSON.parse(errorResponseText);
    } catch {
      errorPayload = { detail: errorResponseText };
    }
  }
  const errorDetail = resolveApiErrorDetail(errorPayload);
  return new ApiRequestError({
    status: response.status,
    message: resolveApiErrorMessage(response.status, errorDetail),
    detail: errorDetail,
  });
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw await buildApiRequestError(response);
  }
  if (response.status === 204 || response.status === 205) {
    return undefined as T;
  }
  const responseText = await response.text();
  if (!responseText) {
    return undefined as T;
  }
  return JSON.parse(responseText) as T;
}

async function performApiRequest(endpoint: string, requestInit?: RequestInit): Promise<Response> {
  return fetch(`${API_BASE}${endpoint}`, {
    credentials: "same-origin",
    ...requestInit,
  });
}

export async function get<T>(endpoint: string, options?: ApiRequestOptions): Promise<T> {
  const response = await performApiRequest(endpoint, { headers: options?.headers });
  return handleResponse<T>(response);
}

export async function post<T>(
  endpoint: string,
  data?: unknown,
  options?: ApiRequestOptions,
): Promise<T> {
  const response = await performApiRequest(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {}),
    },
    body: data ? JSON.stringify(data) : undefined,
  });
  return handleResponse<T>(response);
}

export async function put<T>(
  endpoint: string,
  data: unknown,
  options?: ApiRequestOptions,
): Promise<T> {
  const response = await performApiRequest(endpoint, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {}),
    },
    body: JSON.stringify(data),
  });
  return handleResponse<T>(response);
}

export async function patch<T>(
  endpoint: string,
  data: unknown,
  options?: ApiRequestOptions,
): Promise<T> {
  const response = await performApiRequest(endpoint, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {}),
    },
    body: JSON.stringify(data),
  });
  return handleResponse<T>(response);
}

export async function del<T>(endpoint: string, options?: ApiRequestOptions): Promise<T> {
  const response = await performApiRequest(endpoint, {
    method: "DELETE",
    headers: options?.headers,
  });
  return handleResponse<T>(response);
}

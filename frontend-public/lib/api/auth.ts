import { apiGet, apiPost } from "./client"

/** Authenticated user returned by the auth API. */
export type UserSession = {
  user_id: string
  display_name: string
  email: string
}

/** Payload for user login. */
export type LoginCredentials = {
  identifier: string
  password: string
}

/** Payload for user registration. */
export type RegisterPayload = {
  email: string
  password: string
  displayName: string
}

/** Log in and return the current user session. */
export async function login(credentials: LoginCredentials): Promise<UserSession> {
  return apiPost<UserSession>("/auth/login", credentials)
}

/** Log out the current user. */
export async function logout(): Promise<void> {
  await apiPost<void>("/auth/logout", {})
}

/** Fetch the current user session, if logged in. */
export async function getCurrentSession(): Promise<UserSession> {
  return apiGet<UserSession>("/auth/me")
}

/** Register a new user and return the created session. */
export async function register(payload: RegisterPayload): Promise<UserSession> {
  return apiPost<UserSession>("/auth/register", {
    display_name: payload.displayName,
    email: payload.email,
    password: payload.password,
  })
}

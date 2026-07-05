import { apiGet, apiPost } from './client'

/** Authenticated user returned by the admin auth API. */
export type UserSession = {
  user_id: string
  display_name: string
  username: string
}

/** Payload for admin user login. */
export type LoginCredentials = {
  identifier: string
  password: string
}

/** Log in and return the current admin user session. */
export async function login(credentials: LoginCredentials): Promise<UserSession> {
  return apiPost<UserSession>('/auth/login', credentials)
}

/** Log out the current admin user. */
export async function logout(): Promise<void> {
  await apiPost<void>('/auth/logout', {})
}

/** Fetch the current admin user session, if logged in. */
export async function getCurrentSession(): Promise<UserSession> {
  return apiGet<UserSession>('/auth/me')
}

import { apiGet, apiPost } from './client'

export type PublicUserStatus = 'active' | 'disabled'

export type PublicUser = {
  id: string
  email: string
  display_name: string
  status: PublicUserStatus
  created_at: string | null
}

export type PublicUserListResponse = {
  items: PublicUser[]
  total: number
  page: number
  page_size: number
}

export type ListPublicUsersParams = {
  page?: number
  page_size?: number
  status?: PublicUserStatus
  keyword?: string
}

/** List public users with optional filtering and pagination. */
export async function listPublicUsers(
  params: ListPublicUsersParams = {}
): Promise<PublicUserListResponse> {
  return apiGet<PublicUserListResponse>('/admin/users', { params })
}

/** Get a single public user by ID. */
export async function getPublicUser(userId: string): Promise<PublicUser> {
  return apiGet<PublicUser>(`/admin/users/${userId}`)
}

/** Enable a public user account. */
export async function enablePublicUser(userId: string): Promise<PublicUser> {
  return apiPost<PublicUser>(`/admin/users/${userId}/enable`, {})
}

/** Disable a public user account. */
export async function disablePublicUser(userId: string): Promise<PublicUser> {
  return apiPost<PublicUser>(`/admin/users/${userId}/disable`, {})
}

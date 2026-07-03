import { clearCookies } from '@/test-utils/cookies'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** Dynamically import the auth store to reset module state between tests. */
async function importAuthStore() {
  const { useAuthStore } = await import('./auth-store')
  return useAuthStore
}

const sampleUser = {
  user_id: 'usr-1',
  display_name: 'Alice',
  username: 'alice',
}

describe('useAuthStore', () => {
  beforeEach(() => {
    clearCookies()
    vi.resetModules()
  })

  it('starts unauthenticated when nothing is persisted', async () => {
    const useAuthStore = await importAuthStore()

    expect(useAuthStore.getState().auth.user).toBeNull()
    expect(useAuthStore.getState().auth.isAuthenticated).toBe(false)
  })

  it('persists user so a new store instance reads it back', async () => {
    const useAuthStore = await importAuthStore()
    useAuthStore.getState().auth.setUser(sampleUser)

    vi.resetModules()
    const useAuthStoreAfterReload = await importAuthStore()

    expect(useAuthStoreAfterReload.getState().auth.user).toEqual(sampleUser)
    expect(useAuthStoreAfterReload.getState().auth.isAuthenticated).toBe(true)
  })

  it('updates the signed-in user via setUser', async () => {
    const useAuthStore = await importAuthStore()

    useAuthStore.getState().auth.setUser(sampleUser)

    expect(useAuthStore.getState().auth.user).toEqual(sampleUser)
  })

  it('reset clears user and drops persistence', async () => {
    const useAuthStore = await importAuthStore()
    useAuthStore.getState().auth.setUser(sampleUser)

    useAuthStore.getState().auth.reset()

    expect(useAuthStore.getState().auth.user).toBeNull()
    expect(useAuthStore.getState().auth.isAuthenticated).toBe(false)

    vi.resetModules()
    const useAuthStoreAfterReload = await importAuthStore()

    expect(useAuthStoreAfterReload.getState().auth.user).toBeNull()
    expect(useAuthStoreAfterReload.getState().auth.isAuthenticated).toBe(false)
  })
})

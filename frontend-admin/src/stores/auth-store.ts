import { create } from 'zustand'
import { getCookie, setCookie, removeCookie } from '@/lib/cookies'
import type { UserSession } from '@/api/auth'

const AUTH_USER_KEY = 'admin_auth_user'

interface AuthState {
  auth: {
    user: UserSession | null
    setUser: (user: UserSession | null) => void
    isAuthenticated: boolean
    setIsAuthenticated: (value: boolean) => void
    reset: () => void
  }
}

/** Load the persisted user from cookies, if any. */
function loadInitialUser(): UserSession | null {
  const cookieState = getCookie(AUTH_USER_KEY)
  if (!cookieState) return null
  try {
    return JSON.parse(cookieState) as UserSession
  } catch {
    return null
  }
}

export const useAuthStore = create<AuthState>()((set) => ({
  auth: {
    user: loadInitialUser(),
    setUser: (user) =>
      set((state) => {
        if (user) {
          setCookie(AUTH_USER_KEY, JSON.stringify(user), 60 * 60 * 24 * 7)
        } else {
          removeCookie(AUTH_USER_KEY)
        }
        return { ...state, auth: { ...state.auth, user, isAuthenticated: !!user } }
      }),
    isAuthenticated: !!loadInitialUser(),
    setIsAuthenticated: (value) =>
      set((state) => ({ ...state, auth: { ...state.auth, isAuthenticated: value } })),
    reset: () =>
      set((state) => {
        removeCookie(AUTH_USER_KEY)
        return {
          ...state,
          auth: { ...state.auth, user: null, isAuthenticated: false },
        }
      }),
  },
}))

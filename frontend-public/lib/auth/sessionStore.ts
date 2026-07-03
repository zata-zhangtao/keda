import type { UserSession } from "../api/types";

let cachedUserSession: UserSession | null = null;

export function getCachedUserSession(): UserSession | null {
  return cachedUserSession;
}

export function setCachedUserSession(nextUserSession: UserSession | null): void {
  cachedUserSession = nextUserSession;
}

export function clearCachedUserSession(): void {
  cachedUserSession = null;
}

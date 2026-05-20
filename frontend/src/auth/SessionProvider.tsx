import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

import {
  clearCachedUserSession,
  getCachedUserSession,
  setCachedUserSession,
} from "@shared/auth/sessionStore";
import { getCurrentSession, login, logout } from "@shared/api/auth";
import { ApiRequestError } from "@shared/api/client";
import type { UserSession } from "@shared/api/types";

type SessionStatus = "loading" | "authenticated" | "anonymous" | "error";

type SessionContextValue = {
  status: SessionStatus;
  userSession: UserSession | null;
  sessionRestoreErrorMessage: string | null;
  signIn: (credentials: { identifier: string; password: string }) => Promise<void>;
  signOut: () => Promise<void>;
};

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const cachedSession = getCachedUserSession();
  const [status, setStatus] = useState<SessionStatus>(cachedSession ? "authenticated" : "loading");
  const [userSession, setUserSession] = useState<UserSession | null>(cachedSession);
  const [sessionRestoreErrorMessage, setSessionRestoreErrorMessage] = useState<string | null>(null);

  async function refreshSessionState(): Promise<void> {
    if (!getCachedUserSession()) {
      setStatus("loading");
    }
    setSessionRestoreErrorMessage(null);

    try {
      const nextSession = await getCurrentSession();
      setCachedUserSession(nextSession);
      setUserSession(nextSession);
      setStatus("authenticated");
    } catch (error) {
      if (error instanceof ApiRequestError && error.status === 401) {
        clearCachedUserSession();
        setUserSession(null);
        setStatus("anonymous");
        return;
      }
      clearCachedUserSession();
      setUserSession(null);
      setSessionRestoreErrorMessage(
        error instanceof Error ? error.message : "Failed to restore session.",
      );
      setStatus("error");
    }
  }

  async function signIn(credentials: { identifier: string; password: string }): Promise<void> {
    const nextSession = await login(credentials);
    setCachedUserSession(nextSession);
    setUserSession(nextSession);
    setSessionRestoreErrorMessage(null);
    setStatus("authenticated");
  }

  async function signOut(): Promise<void> {
    await logout();
    clearCachedUserSession();
    setUserSession(null);
    setSessionRestoreErrorMessage(null);
    setStatus("anonymous");
  }

  useEffect(() => {
    void refreshSessionState();
  }, []);

  return (
    <SessionContext.Provider
      value={{ status, userSession, sessionRestoreErrorMessage, signIn, signOut }}
    >
      {children}
    </SessionContext.Provider>
  );
}

export function useSession(): SessionContextValue {
  const sessionContext = useContext(SessionContext);
  if (!sessionContext) {
    throw new Error("useSession must be used inside SessionProvider.");
  }
  return sessionContext;
}

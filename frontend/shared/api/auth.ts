// TODO: 替换为实际后端认证接口路径

import { get, post } from "./client";
import type { UserSession } from "./types";

export async function getCurrentSession(): Promise<UserSession> {
  return get<UserSession>("/auth/me");
}

export async function login(credentials: {
  identifier: string;
  password: string;
}): Promise<UserSession> {
  return post<UserSession>("/auth/login", credentials);
}

export async function logout(): Promise<void> {
  return post<void>("/auth/logout");
}

/** /auth/* calls. Shapes match app/schemas/auth.py exactly. */

import { api } from "./client";
import type { TokenPair, UserOut } from "../types";

export async function register(email: string, password: string): Promise<UserOut> {
  return api<UserOut>("/auth/register", {
    method: "POST",
    body: { email, password },
    auth: false,
  });
}

export async function login(email: string, password: string): Promise<TokenPair> {
  return api<TokenPair>("/auth/login", {
    method: "POST",
    body: { email, password },
    auth: false,
  });
}

export async function me(): Promise<UserOut> {
  return api<UserOut>("/auth/me");
}

/** Idempotent RFC 7009: always 204. The refresh token goes in the BODY —
 * no Authorization header involved. */
export async function logout(refreshToken: string | null): Promise<void> {
  if (!refreshToken) return;
  await api("/auth/logout", {
    method: "POST",
    body: { refresh_token: refreshToken },
    auth: false,
  });
}

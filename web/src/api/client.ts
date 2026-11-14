/** Fetch wrapper: one place for the base URL, bearer header, JSON envelope
 * parsing, and the single-flight 401 → refresh → retry dance.
 *
 * Contract notes from the backend:
 * - every error is {"error": {"code","message","details"?}, "request_id"};
 * - /auth/refresh rotates the token (single use) → we MUST persist the new
 *   pair it returns;
 * - 204 responses have no body. */

import { session } from "./session";
import type { ApiErrorBody, TokenPair } from "../types";

export const API_URL: string =
  import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  code: string;
  details?: Record<string, unknown>;
  requestId?: string;

  constructor(
    status: number,
    code: string,
    message: string,
    details?: Record<string, unknown>,
    requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError;
}

/** One refresh at a time: parallel 401s all await the SAME promise instead of
 * burning several rotation tokens (each refresh is single-use!). */
let refreshInFlight: Promise<boolean> | null = null;

async function refreshOnce(): Promise<boolean> {
  const rt = session.refresh;
  if (!rt) return false;
  try {
    const resp = await fetch(`${API_URL}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: rt }),
    });
    if (!resp.ok) return false;
    const pair = (await resp.json()) as TokenPair;
    session.set(pair.access_token, pair.refresh_token);
    return true;
  } catch {
    return false;
  }
}

interface Options {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** Attach the bearer header (default true). logout/refresh don't need it. */
  auth?: boolean;
  /** Internal: replay-after-refresh already used once → never loop. */
  _retried?: boolean;
}

export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const { method = "GET", body, signal, auth = true, _retried = false } = opts;

  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth && session.access) headers.Authorization = `Bearer ${session.access}`;

  let resp: Response;
  try {
    resp = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "network_error", `Cannot reach API at ${API_URL}`);
  }

  // 401 mid-session → try ONE refresh, then replay the request ONCE.
  if (resp.status === 401 && auth && !_retried && session.refresh) {
    if (!refreshInFlight) {
      refreshInFlight = refreshOnce().finally(() => {
        refreshInFlight = null;
      });
    }
    const ok = await refreshInFlight;
    if (ok) return api<T>(path, { ...opts, _retried: true });
    // Dead refresh token → the session is over (reuse detection may have
    // nuked the family). Clear storage; AuthContext shows the login screen.
    session.clear();
  }

  if (resp.status === 204) return undefined as T;

  const text = await resp.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!resp.ok) {
    const envelope = payload as ApiErrorBody | null;
    const e = envelope?.error;
    throw new ApiError(
      resp.status,
      e?.code ?? "unknown",
      e?.message ?? `HTTP ${resp.status}`,
      e?.details,
      envelope?.request_id,
    );
  }
  return payload as T;
}

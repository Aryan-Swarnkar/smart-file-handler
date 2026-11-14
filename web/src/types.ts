/** Types mirroring the backend's Pydantic schemas (app/schemas/*.py) and the
 * unified error envelope (app/core/errors.py). Keep in sync manually — a
 * generated client would be overkill for this testing UI. */

export type FileKind = "image" | "pdf" | "document" | "text" | "other";
export type FileStatus = "uploaded" | "processing" | "done" | "failed";

/** GET /auth/me, POST /auth/register */
export interface UserOut {
  id: string;
  email: string;
  created_at: string;
}

/** POST /auth/login, POST /auth/refresh */
export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

/** GET /files/{id}, PATCH /files/{id} */
export interface FileOut {
  id: string;
  original_name: string;
  mime_type: string;
  size_bytes: number;
  sha256: string | null;
  status: FileStatus;
  kind: FileKind | null;
  doc_topic: string | null;
  created_at: string;
}

/** POST /files/{id}/complete */
export interface FileCompleteOut extends FileOut {
  deduplicated: boolean;
}

/** POST /files/init */
export interface FileInitOut {
  file_id: string;
  upload_url: string;
  expires_in: number;
}

/** GET /files/{id}/url */
export interface FileUrlOut {
  url: string;
  expires_in: number;
}

/** GET /files */
export interface FilePageOut {
  items: FileOut[];
  next_cursor: string | null;
}

/** Every non-2xx response body: {"error": {...}, "request_id": "..."} */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
  request_id?: string;
}

/** GET /readyz */
export interface ReadyBody {
  status: "ready" | "unavailable";
  checks: Record<string, string>;
}

// --- formatting helpers (shared) ------------------------------------------------

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KiB", "MiB", "GiB"];
  let v = n;
  let u = "B";
  for (const next of units) {
    if (v < 1024) break;
    v /= 1024;
    u = next;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${u}`;
}

export function fmtDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

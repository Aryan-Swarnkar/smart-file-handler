/** /files/* calls. Shapes match app/schemas/files.py exactly. */

import { api } from "./client";
import type {
  FileCompleteOut,
  FileInitOut,
  FileOut,
  FilePageOut,
  FileUrlOut,
} from "../types";

export async function initUpload(file: {
  name: string;
  type: string;
  size: number;
}): Promise<FileInitOut> {
  return api<FileInitOut>("/files/init", {
    method: "POST",
    body: {
      filename: file.name,
      content_type: file.type || "application/octet-stream",
      size: file.size,
    },
  });
}

/** Complete AFTER the bytes are PUT to the presigned URL. Idempotent
 * server-side; deduplicated=true means the duplicate object was discarded. */
export async function completeUpload(fileId: string): Promise<FileCompleteOut> {
  return api<FileCompleteOut>(`/files/${fileId}/complete`, { method: "POST" });
}

export interface ListParams {
  limit?: number;
  cursor?: string | null;
  kind?: string | null;
  doc_topic?: string | null;
  status?: string | null;
}

/** Keyset pagination: pass next_cursor back verbatim; empty params → first
 * page. Any malformed cursor comes back as 422 bad_cursor (surfaced). */
export async function listFiles(params: ListParams = {}): Promise<FilePageOut> {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.cursor) qs.set("cursor", params.cursor);
  if (params.kind) qs.set("kind", params.kind);
  if (params.doc_topic) qs.set("doc_topic", params.doc_topic);
  if (params.status) qs.set("status", params.status);
  const q = qs.toString();
  return api<FilePageOut>(`/files${q ? `?${q}` : ""}`);
}

export async function getFileUrl(fileId: string, download: boolean): Promise<FileUrlOut> {
  return api<FileUrlOut>(
    `/files/${fileId}/url${download ? "?download=true" : ""}`,
  );
}

export async function renameFile(fileId: string, originalName: string): Promise<FileOut> {
  return api<FileOut>(`/files/${fileId}`, {
    method: "PATCH",
    body: { original_name: originalName },
  });
}

export async function deleteFile(fileId: string): Promise<void> {
  return api<void>(`/files/${fileId}`, { method: "DELETE" }); // 204
}

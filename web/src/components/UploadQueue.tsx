/** The upload queue: per-file state machine with XHR progress, cancel,
 * bounded parallelism (3), and dedup/oversize badges.
 *
 * State lives in a ref-mirror (itemsRef) because async continuations must
 * read/patch the LATEST list, not a stale closure; setItems mirrors it for
 * render. Per file: init → PUT (0-90%) → complete → done | dedup | failed. */

import { useRef, useState } from "react";
import { completeUpload, initUpload } from "../api/files";
import { ApiError, isApiError } from "../api/client";
import { putToPresigned } from "../api/upload";
import type { FileCompleteOut } from "../types";
import { fmtBytes } from "../types";
import { toastError } from "./Toaster";

/** Mirrors MAX_UPLOAD_MB default. The server re-verifies and its 422
 * details.max_bytes is authoritative — this is a courtesy pre-check. */
const MAX_MB = 25;
const CONCURRENCY = 3;

export type QueueStatus =
  | "waiting"
  | "uploading"
  | "finalizing"
  | "done"
  | "dedup"
  | "canceled"
  | "failed";

export interface QueueItem {
  key: string;
  file: File;
  status: QueueStatus;
  progress: number; // 0..1 PUT progress
  error?: ApiError;
  result?: FileCompleteOut;
}

const STATUS_ICON: Record<QueueStatus, string> = {
  waiting: "⏳",
  uploading: "📤",
  finalizing: "🧮",
  done: "✅",
  dedup: "♻️",
  canceled: "🚫",
  failed: "⛔",
};

/** Hook: owns the queue (state in a ref-mirror so async continuations always
 * see the latest list). App wires Dropzone.onFiles → addFiles and renders
 * <UploadQueueView {...queue} />. */
export function useUploadQueue(onUploaded: () => void) {
  const [items, setItems] = useState<QueueItem[]>([]);
  const itemsRef = useRef<QueueItem[]>([]);
  const controllers = useRef(new Map<string, AbortController>());
  const activeRef = useRef(0);
  const uploadedRef = useRef(onUploaded);
  uploadedRef.current = onUploaded;

  function patch(key: string, changes: Partial<QueueItem>): void {
    itemsRef.current = itemsRef.current.map((i) =>
      i.key === key ? { ...i, ...changes } : i,
    );
    setItems(itemsRef.current);
  }

  function pump(): void {
    while (activeRef.current < CONCURRENCY) {
      const next = itemsRef.current.find((i) => i.status === "waiting");
      if (!next) return;
      next.status = "uploading"; // claim it in the ref before anyone else scans
      patch(next.key, { status: "uploading" });
      activeRef.current++;
      void run(next);
    }
  }

  async function run(item: QueueItem): Promise<void> {
    const controller = new AbortController();
    controllers.current.set(item.key, controller);
    try {
      const init = await initUpload(item.file);
      const handle = putToPresigned(init.upload_url, item.file, (fraction) =>
        patch(item.key, { progress: fraction }),
      );
      controller.signal.addEventListener("abort", () => handle.abort());
      await handle.promise;

      patch(item.key, { status: "finalizing", progress: 1 });
      const result = await completeUpload(init.file_id);
      patch(item.key, {
        status: result.deduplicated ? "dedup" : "done",
        result,
        error: undefined,
      });
      uploadedRef.current();
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        patch(item.key, { status: "canceled" });
      } else if (isApiError(err)) {
        patch(item.key, { status: "failed", error: err });
        toastError(err);
      } else {
        const wrapped = new ApiError(0, "unknown", String(err));
        patch(item.key, { status: "failed", error: wrapped });
        toastError(wrapped);
      }
    } finally {
      controllers.current.delete(item.key);
      activeRef.current--;
      pump();
    }
  }

  function addFiles(files: File[]): void {
    const stamp = Date.now();
    const fresh: QueueItem[] = files.map((file, i) => {
      const oversized = file.size > MAX_MB * 1024 * 1024;
      return {
        key: `${stamp}-${i}-${file.name}`,
        file,
        status: oversized ? "failed" : "waiting",
        progress: 0,
        error: oversized
          ? new ApiError(
              0,
              "file_too_large",
              `${fmtBytes(file.size)} exceeds the ${MAX_MB} MB limit`,
            )
          : undefined,
      };
    });
    itemsRef.current = [...itemsRef.current, ...fresh];
    setItems(itemsRef.current);
    pump();
  }

  function cancel(key: string): void {
    controllers.current.get(key)?.abort();
  }

  function retryFailed(): void {
    let any = false;
    itemsRef.current = itemsRef.current.map((i) => {
      if (i.status !== "failed") return i;
      any = true;
      return { ...i, status: "waiting", error: undefined, progress: 0 };
    });
    if (any) {
      setItems(itemsRef.current);
      pump();
    }
  }

  function clearFinished(): void {
    itemsRef.current = itemsRef.current.filter(
      (i) => !["done", "dedup", "canceled"].includes(i.status),
    );
    setItems(itemsRef.current);
  }

  return { items, addFiles, cancel, retryFailed, clearFinished };
}

export type UploadQueueApi = ReturnType<typeof useUploadQueue>;

export function UploadQueueView({ queue }: { queue: UploadQueueApi }): React.JSX.Element {
  const { items, cancel, retryFailed, clearFinished } = queue;
  const failedCount = items.filter((i) => i.status === "failed").length;
  const finishedCount = items.filter((i) =>
    ["done", "dedup", "canceled"].includes(i.status),
  ).length;

  return (
    <section className="queue">
      {items.length === 0 ? (
        <div className="queue-empty">No uploads in flight.</div>
      ) : (
        <ul>
          {items.map((item) => (
            <li key={item.key} className={`queue-item ${item.status}`}>
              <span className="qi-icon" title={item.status}>
                {STATUS_ICON[item.status]}
              </span>
              <div className="qi-body">
                <div className="qi-name" title={item.file.name}>
                  {item.file.name}
                </div>
                <div className="qi-meta">
                  {fmtBytes(item.file.size)}
                  {item.status === "uploading" &&
                    ` — ${Math.round(item.progress * 100)}%`}
                  {item.status === "finalizing" && " — hashing + sniffing…"}
                  {item.status === "dedup" && " — ♻️ duplicate: linked to the existing file"}
                  {item.error && ` — ${item.error.code}: ${item.error.message}`}
                </div>
                {(item.status === "uploading" || item.status === "finalizing") && (
                  <div className="qi-bar">
                    <div
                      className={item.status === "finalizing" ? "indeterminate" : "fill"}
                      style={
                        item.status === "uploading"
                          ? { width: `${item.progress * 100}%` }
                          : undefined
                      }
                    />
                  </div>
                )}
              </div>
              {["waiting", "uploading", "finalizing"].includes(item.status) && (
                <button className="btn small" onClick={() => cancel(item.key)}>
                  Cancel
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {(failedCount > 0 || finishedCount > 0) && (
        <div className="queue-actions">
          {failedCount > 0 && (
            <button className="btn" onClick={retryFailed}>
              Retry failed ({failedCount})
            </button>
          )}
          {finishedCount > 0 && (
            <button className="btn ghost" onClick={clearFinished}>
              Clear finished ({finishedCount})
            </button>
          )}
        </div>
      )}
    </section>
  );
}

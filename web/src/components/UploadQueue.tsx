/** The upload queue: per-file state machine with XHR progress, cancel,
 * bounded parallelism (3), and dedup/oversize badges.
 *
 * State lives in a ref-mirror (itemsRef) because async continuations must
 * read/patch the LATEST list, not a stale closure; setItems mirrors it for
 * render. Per file: init → PUT (0-90%) → complete → done | dedup | failed.
 *
 * STABILITY (the "shaking UI" fix) — a 2000-file folder drop used to render
 * 2000 rows re-rendering on every progress tick, which saturated the main
 * thread and made the page jump around as rows appeared/grew/shrank:
 * - Rendering is BOUNDED: active rows are capped (with an "…and N more"
 *   line), failed rows are capped, everything finished collapses into ONE
 *   summary row. DOM size stays flat no matter how big the queue gets.
 * - Row heights are RESERVED: the progress bar and the actions row render
 *   "idle" (invisible, space kept) instead of appearing/disappearing, so
 *   status transitions don't push the page up and down.
 * - Progress patches are throttled (~8/s) — XHR fires progress far more
 *   often than any human can watch.
 * - Rows are memoized; only the patched row re-renders.
 * - Empty (0-byte) files fail instantly client-side (the server would 422
 *   them anyway) — no pointless network round trip per junk file.
 */

import { memo, useCallback, useRef, useState } from "react";
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
/** Bounded rendering: failed rows are capped in the DOM. */
const MAX_FAILED_ROWS = 20;
/** Progress → state patches at most ~8/s (XHR fires way more often). */
const PROGRESS_THROTTLE_MS = 125;

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
 * <UploadQueueView {...queue} />. Every returned function is ref-backed and
 * memoized, so QueueRow's memoization actually bails. */
export function useUploadQueue(onUploaded: () => void) {
  const [items, setItems] = useState<QueueItem[]>([]);
  const itemsRef = useRef<QueueItem[]>([]);
  const controllers = useRef(new Map<string, AbortController>());
  const activeRef = useRef(0);
  const uploadedRef = useRef(onUploaded);
  uploadedRef.current = onUploaded;

  const patch = useCallback((key: string, changes: Partial<QueueItem>): void => {
    itemsRef.current = itemsRef.current.map((i) =>
      i.key === key ? { ...i, ...changes } : i,
    );
    setItems(itemsRef.current);
  }, []);

  const run = useCallback(
    async (item: QueueItem): Promise<void> => {
      const controller = new AbortController();
      controllers.current.set(item.key, controller);
      try {
        const init = await initUpload(item.file);
        // A cancel clicked while initUpload was in flight fired BEFORE any
        // abort listener existed (the listener is registered below, after the
        // handle exists) — the signal is already aborted here, so honor it
        // now: no bytes are ever moved, the init row is left for the worker
        // sweep (by design, same as a mid-PUT cancel).
        if (controller.signal.aborted) {
          patch(item.key, { status: "canceled" });
          return;
        }
        let lastPaint = 0;
        const handle = putToPresigned(init.upload_url, item.file, (fraction) => {
          const now = Date.now();
          if (fraction >= 1 || now - lastPaint >= PROGRESS_THROTTLE_MS) {
            lastPaint = now;
            patch(item.key, { progress: fraction });
          }
        });
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
    },
    [patch],
  );

  const pump = useCallback((): void => {
    while (activeRef.current < CONCURRENCY) {
      const next = itemsRef.current.find((i) => i.status === "waiting");
      if (!next) return;
      next.status = "uploading"; // claim it in the ref before anyone else scans
      patch(next.key, { status: "uploading" });
      activeRef.current++;
      void run(next);
    }
  }, [patch, run]);

  const addFiles = useCallback((files: File[]): void => {
    const stamp = Date.now();
    const fresh: QueueItem[] = files.map((file, i) => {
      // Uniform error TEXT per failure kind: identical failures dedupe into
      // ONE toast (the per-file size is already visible in the queue row).
      if (file.size === 0) {
        const err = new ApiError(0, "file_empty", "Empty files cannot be uploaded");
        toastError(err); // deduped by the Toaster across the whole batch
        return {
          key: `${stamp}-${i}-${file.name}`,
          file,
          status: "failed",
          progress: 0,
          error: err,
        };
      }
      const oversized = file.size > MAX_MB * 1024 * 1024;
      if (oversized) {
        const err = new ApiError(0, "file_too_large", `File exceeds the ${MAX_MB} MB limit.`);
        toastError(err); // deduped by the Toaster across the whole batch
        return {
          key: `${stamp}-${i}-${file.name}`,
          file,
          status: "failed",
          progress: 0,
          error: err,
        };
      }
      return {
        key: `${stamp}-${i}-${file.name}`,
        file,
        status: "waiting",
        progress: 0,
      };
    });
    itemsRef.current = [...itemsRef.current, ...fresh];
    setItems(itemsRef.current);
    pump();
  }, [pump]);

  const cancel = useCallback(
    (key: string): void => {
      const controller = controllers.current.get(key);
      if (controller) {
        controller.abort();
        return;
      }
      // No controller ⇒ the item has not been claimed yet: mark it canceled
      // directly so pump() skips it. (It can only be "waiting" here — claimed
      // items always have a controller, created synchronously by run().)
      const item = itemsRef.current.find((i) => i.key === key);
      if (item?.status === "waiting") patch(key, { status: "canceled" });
    },
    [patch],
  );

  const retryFailed = useCallback((): void => {
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
  }, [pump]);

  const clearFinished = useCallback((): void => {
    itemsRef.current = itemsRef.current.filter(
      (i) => !["done", "dedup", "canceled"].includes(i.status),
    );
    setItems(itemsRef.current);
  }, []);

  return { items, addFiles, cancel, retryFailed, clearFinished };
}

export type UploadQueueApi = ReturnType<typeof useUploadQueue>;

const QueueRow = memo(function QueueRow({
  item,
  onCancel,
}: {
  item: QueueItem;
  onCancel(key: string): void;
}): React.JSX.Element {
  const busy = item.status === "uploading" || item.status === "finalizing";
  return (
    <li className={`queue-item ${item.status}`}>
      <span className="qi-icon" title={item.status}>
        {STATUS_ICON[item.status]}
      </span>
      <div className="qi-body">
        <div className="qi-name" title={item.file.name}>
          {item.file.name}
        </div>
        <div className="qi-meta">
          {fmtBytes(item.file.size)}
          {item.status === "uploading" && ` — ${Math.round(item.progress * 100)}%`}
          {item.status === "finalizing" && " — hashing + sniffing…"}
          {item.error && ` — ${item.error.code}: ${item.error.message}`}
        </div>
        {/* ALWAYS rendered — idle rows keep the height so transitions don't
            shift the page. Visibility is what changes, never existence. */}
        <div className={`qi-bar${busy ? "" : " idle"}`}>
          <div
            className={item.status === "finalizing" ? "indeterminate" : "fill"}
            style={
              item.status === "uploading" ? { width: `${item.progress * 100}%` } : undefined
            }
          />
        </div>
      </div>
      {/* No Cancel during "finalizing": the bytes are already stored, so a
          cancel there would silently do nothing — better to not offer it.
          Row height is fixed, so the button appearing/disappearing moves
          nothing. */}
      {(item.status === "uploading" || item.status === "waiting") && (
        <button className="btn small" onClick={() => onCancel(item.key)}>
          Cancel
        </button>
      )}
    </li>
  );
});

/** Every queue row is the same height (reserved bar + nowrap meta), so the
 * strip can swap CONTENT between slots without ever resizing. */
const QUEUE_SLOTS = 5;

export function UploadQueueView({ queue }: { queue: UploadQueueApi }): React.JSX.Element {
  const { items, cancel, retryFailed, clearFinished } = queue;
  // High-water mark of how many slot rows this queue lifetime has shown; the
  // strip may grow to it but never shrink back down until the queue is idle.
  const slotFloorRef = useRef(0);

  const pending = items.filter((i) =>
    ["waiting", "uploading", "finalizing"].includes(i.status),
  );
  const failed = items.filter((i) => i.status === "failed");
  const finished = items.filter((i) =>
    ["done", "dedup", "canceled"].includes(i.status),
  );
  const doneCount = finished.filter((i) => i.status === "done").length;
  const dedupCount = finished.filter((i) => i.status === "dedup").length;
  const canceledCount = finished.filter((i) => i.status === "canceled").length;
  const idle = pending.length === 0 && failed.length === 0 && finished.length === 0;

  if (idle) {
    slotFloorRef.current = 0; // reset the reservation when the queue empties
    return (
      <section className="queue">
        <div className="queue-empty">No uploads in flight.</div>
      </section>
    );
  }

  // Slot count GROWS with demand but never shrinks while the section is
  // visible: removing rows mid-drain is exactly what used to shake the page.
  // A 1-file upload renders 1 slot (not 5 blank ones); a 45-file drain keeps
  // its 5 slots to the end and collapses only on "Clear finished".
  const wanted = Math.min(QUEUE_SLOTS, Math.max(pending.length, slotFloorRef.current));
  slotFloorRef.current = wanted;
  const slots: (QueueItem | null)[] = pending.slice(0, wanted);
  while (slots.length < wanted) slots.push(null);

  return (
    <section className="queue">
      <ul>
        {slots.map((item, i) =>
          item ? (
            <QueueRow key={item.key} item={item} onCancel={cancel} />
          ) : (
            <li className="queue-item slot-idle" key={`idle-${i}`} aria-hidden="true">
              <span className="qi-icon">·</span>
              <div className="qi-body">
                <div className="qi-name">&nbsp;</div>
                <div className="qi-meta">&nbsp;</div>
                <div className="qi-bar idle" />
              </div>
            </li>
          ),
        )}
      </ul>
      {/* Always rendered (idle = reserved space) so it never pops in/out and
          shift the grid when the pending count crosses the slot count. */}
      <div className={`queue-more${pending.length <= wanted ? " idle" : ""}`}>
        {pending.length > wanted
          ? `…and ${pending.length - wanted} more in the queue (${pending.length} pending)`
          : "queue drained"}
      </div>

      {/* Counts + clear — always rendered while non-idle; the numbers change,
          the height never does. Completed rows collapse into these counts
          (per-file results stay visible in the grid / failed section). */}
      <div className="queue-counts">
        <span>
          {doneCount > 0 && `✅ ${doneCount} done`}
          {dedupCount > 0 && ` · ♻️ ${dedupCount} dup`}
          {canceledCount > 0 && ` · 🚫 ${canceledCount} canceled`}
          {finished.length === 0 && "nothing finished yet"}
        </span>
        {finished.length > 0 && (
          <button className="btn small ghost" onClick={clearFinished}>
            Clear finished ({finished.length})
          </button>
        )}
      </div>

      {/* The failed group's container is ALWAYS rendered while the queue is
          non-idle; when there are no failures it idles invisibly (height
          reserved) instead of popping in and pushing the grid down. */}
      <div className={`queue-group-label${failed.length === 0 ? " idle" : ""}`}>
        failed ({failed.length})
      </div>
      {failed.length > 0 && (
        <ul>
          {failed.slice(0, MAX_FAILED_ROWS).map((item) => (
            <QueueRow key={item.key} item={item} onCancel={cancel} />
          ))}
        </ul>
      )}
      {failed.length > MAX_FAILED_ROWS && (
        <div className="queue-more">…and {failed.length - MAX_FAILED_ROWS} more failed</div>
      )}

      {/* Reserved action row: visibility (not existence) toggles with the
          counts, so the height never jumps. */}
      <div className={`queue-actions${failed.length === 0 ? " idle" : ""}`}>
        {failed.length > 0 && (
          <button className="btn" onClick={retryFailed}>
            Retry failed ({failed.length})
          </button>
        )}
      </div>
    </section>
  );
}

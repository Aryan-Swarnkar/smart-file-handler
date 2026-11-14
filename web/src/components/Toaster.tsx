/** Tiny event-based toasts: any module can push an error without prop
 * drilling; one <Toaster/> renders the stack. 429s render a live countdown
 * using details.retry_after_seconds from our envelope.
 *
 * Stability rules (the "shaking UI" fix):
 * - Identical toasts (same kind+title+detail) BUMP the existing row (×N
 *   counter + timer restart) instead of appending another — a 45-file failing
 *   batch produces ONE toast, not 45.
 * - Newest-first DOM order in a normal column: the stack is bottom-anchored,
 *   so a new toast takes the fresh space ABOVE the older rows and the cap
 *   evicts the OLDEST row (the bottom-most one). Nothing existing ever moves
 *   on add, eviction, or timeout. Only a manual mid-stack dismissal can shift
 *   older rows slightly.
 * - Per-toast auto-dismiss timers (restarted on bump), managed through a ref
 *   map — never inside state updaters (StrictMode double-invokes those).
 */

import { memo, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, isApiError } from "../api/client";
import type { ApiErrorBody } from "../types";

export interface Toast {
  id: number;
  kind: "error" | "info";
  title: string;
  detail?: string;
  countdown?: number; // seconds remaining (429)
  count?: number; // how many identical events bumped this row
}

let nextId = 1;
const EVENT = "sfh-toast";
const TOAST_MS = 6000;
const MAX_TOASTS = 5;

interface ToastPayload {
  kind: Toast["kind"];
  title: string;
  detail?: string;
  countdown?: number;
}

function push(kind: Toast["kind"], title: string, detail?: string, countdown?: number): void {
  window.dispatchEvent(
    new CustomEvent<ToastPayload>(EVENT, { detail: { kind, title, detail, countdown } }),
  );
}

export function toastError(err: unknown): void {
  if (isApiError(err)) {
    push(
      "error",
      `${err.code} (HTTP ${err.status || "?"})`,
      err.requestId ? `${err.message} — request_id: ${err.requestId}` : err.message,
      err.status === 429 ? countdownFrom(err) : undefined,
    );
  } else if (err instanceof Error) {
    push("error", "error", err.message);
  } else {
    push("error", "error", String(err));
  }
}

export function toastInfo(title: string, detail?: string): void {
  push("info", title, detail);
}

function countdownFrom(err: ApiError): number | undefined {
  const v = err.details?.["retry_after_seconds"];
  return typeof v === "number" ? v : undefined;
}

/** Identity for dedupe: same kind + title + detail ⇒ the same event repeated.
 * The request_id suffix is stripped — every server rejection carries a unique
 * id, which would otherwise defeat dedupe entirely (one representative id is
 * still shown in the row's detail text). */
function keyOf(t: { kind: string; title: string; detail?: string }): string {
  const detail = (t.detail ?? "").replace(/ — request_id: .*$/, "");
  return `${t.kind}|${t.title}|${detail}`;
}

export function Toaster(): React.JSX.Element {
  const [toasts, setToasts] = useState<Toast[]>([]);
  // toastsRef is the AUTHORITATIVE list, mutated synchronously in the event
  // handler — a burst of identical events in one tick must see each other
  // (a render-mirror lags behind and would create one row per event).
  // setToasts merely mirrors the ref for render (UploadQueue's pattern).
  const toastsRef = useRef<Toast[]>([]);
  const timersRef = useRef(new Map<number, number>());

  const sync = useCallback((): void => {
    setToasts(toastsRef.current);
  }, []);

  const armTimer = useCallback((id: number): void => {
    const prev = timersRef.current.get(id);
    if (prev !== undefined) window.clearTimeout(prev);
    timersRef.current.set(
      id,
      window.setTimeout(() => dismiss(id), TOAST_MS),
    );
  }, []);

  const dropTimer = useCallback((id: number): void => {
    const handle = timersRef.current.get(id);
    if (handle !== undefined) {
      window.clearTimeout(handle);
      timersRef.current.delete(id);
    }
  }, []);

  const dismiss = useCallback((id: number): void => {
    dropTimer(id);
    toastsRef.current = toastsRef.current.filter((t) => t.id !== id);
    sync();
  }, [dropTimer, sync]);

  useEffect(() => {
    const onToast = (e: Event): void => {
      const t = (e as CustomEvent<ToastPayload>).detail;
      if (t.countdown !== undefined) {
        // 429 countdown rows are never deduped — each carries a live timer of
        // its own. Newest first (see ordering note below).
        toastsRef.current = [{ id: nextId++, ...t }, ...toastsRef.current].slice(0, MAX_TOASTS);
        sync();
        return;
      }
      const existing = toastsRef.current.find((x) => keyOf(x) === keyOf(t));
      if (existing) {
        armTimer(existing.id); // identical event: restart the row's window
        toastsRef.current = toastsRef.current.map((x) =>
          x.id === existing.id ? { ...x, count: (x.count ?? 1) + 1 } : x,
        );
      } else {
        const added: Toast = { id: nextId++, ...t, count: 1 };
        armTimer(added.id);
        // Newest FIRST in the DOM. The .toaster is a bottom-anchored column:
        // the new row takes the space above the older ones, and the size cap
        // evicts the LAST element — the oldest, bottom-most row. Both
        // operations move nothing that is already on screen.
        const next = [added, ...toastsRef.current];
        if (next.length > MAX_TOASTS) {
          for (let i = MAX_TOASTS; i < next.length; i++) dropTimer(next[i].id);
          toastsRef.current = next.slice(0, MAX_TOASTS);
        } else {
          toastsRef.current = next;
        }
      }
      sync();
    };
    window.addEventListener(EVENT, onToast);
    return () => window.removeEventListener(EVENT, onToast);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [armTimer, dismiss, dropTimer, sync]);

  return (
    <div className="toaster">
      {toasts.map((t) => (
        <ToastRow key={t.id} toast={t} onDismiss={dismiss} />
      ))}
    </div>
  );
}

const ToastRow = memo(function ToastRow({
  toast,
  onDismiss,
}: {
  toast: Toast;
  onDismiss: (id: number) => void;
}): React.JSX.Element {
  const [remaining, setRemaining] = useState(toast.countdown);

  useEffect(() => {
    if (remaining === undefined) return;
    if (remaining <= 0) {
      onDismiss(toast.id);
      return;
    }
    const timer = window.setTimeout(() => setRemaining(remaining - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [remaining, toast.id, onDismiss]);

  return (
    <div className={`toast ${toast.kind}`} role="alert">
      <div className="toast-title">
        {toast.kind === "error" ? "⛔ " : "ℹ️ "}
        {toast.title}
        {(toast.count ?? 1) > 1 && <span className="toast-count">×{toast.count}</span>}
        {remaining !== undefined && remaining > 0 && (
          <span className="toast-countdown">retry in {remaining}s</span>
        )}
      </div>
      {toast.detail && <div className="toast-detail">{toast.detail}</div>}
      <button className="toast-close" onClick={() => onDismiss(toast.id)} aria-label="dismiss">
        ×
      </button>
    </div>
  );
});

/** Helper for the empty-envelope case (e.g. plain fetch responses). */
export function toastFromBody(body: ApiErrorBody): void {
  push("error", body.error?.code ?? "error", body.request_id ? `${body.error?.message} (${body.request_id})` : body.error?.message);
}

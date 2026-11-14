/** Tiny event-based toasts: any module can push an error without prop
 * drilling; one <Toaster/> renders the stack. 429s render a live countdown
 * using details.retry_after_seconds from our envelope. */

import { useEffect, useState } from "react";
import { ApiError, isApiError } from "../api/client";
import type { ApiErrorBody } from "../types";

export interface Toast {
  id: number;
  kind: "error" | "info";
  title: string;
  detail?: string;
  countdown?: number; // seconds remaining (429)
}

let nextId = 1;
const EVENT = "sfh-toast";

function push(kind: Toast["kind"], title: string, detail?: string, countdown?: number): void {
  window.dispatchEvent(
    new CustomEvent<Toast>(EVENT, { detail: { id: nextId++, kind, title, detail, countdown } }),
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

export function Toaster(): React.JSX.Element {
  const [toasts, setToasts] = useState<Toast[]>([]);

  useEffect(() => {
    const onToast = (e: Event): void => {
      const t = (e as CustomEvent<Toast>).detail;
      setToasts((prev) => [...prev.slice(-4), t]);
      if (t.countdown === undefined) {
        window.setTimeout(() => dismiss(t.id), 6000);
      }
    };
    window.addEventListener(EVENT, onToast);
    return () => window.removeEventListener(EVENT, onToast);
  }, []);

  function dismiss(id: number): void {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }

  return (
    <div className="toaster">
      {toasts.map((t) => (
        <ToastRow key={t.id} toast={t} onDismiss={dismiss} />
      ))}
    </div>
  );
}

function ToastRow({ toast, onDismiss }: { toast: Toast; onDismiss: (id: number) => void }): React.JSX.Element {
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
}

/** Helper for the empty-envelope case (e.g. plain fetch responses). */
export function toastFromBody(body: ApiErrorBody): void {
  push("error", body.error?.code ?? "error", body.request_id ? `${body.error?.message} (${body.request_id})` : body.error?.message);
}

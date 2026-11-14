/** File browser: card grid over the REAL keyset pagination (cursor + Load
 * more), kind/status/topic filters, and per-file actions: view (presigned
 * URL), download (Content-Disposition attachment), rename, hard delete. */

import { useCallback, useEffect, useRef, useState } from "react";
import { deleteFile, getFileUrl, listFiles, renameFile } from "../api/files";
import type { FileOut } from "../types";
import { fmtBytes, fmtDate } from "../types";
import { toastError } from "./Toaster";

const KIND_ICON: Record<string, string> = {
  image: "🖼️",
  pdf: "📕",
  document: "📄",
  text: "📝",
  other: "📦",
};

const STATUS_CLASS: Record<string, string> = {
  uploaded: "chip uploaded",
  processing: "chip processing",
  done: "chip done",
  failed: "chip failed",
};

const PAGE = 20;

interface Filters {
  kind: string;
  status: string;
  topic: string;
}

const EMPTY_FILTERS: Filters = { kind: "", status: "", topic: "" };

interface Props {
  /** Bump to refetch page 1 (parent bumps it after uploads complete). */
  refreshSignal: number;
}

export function FileGrid({ refreshSignal }: Props): React.JSX.Element {
  const [items, setItems] = useState<FileOut[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  // Stale-response guard: rapid filter changes leave several fetches in
  // flight; only the response of the LATEST fetch may touch state.
  const seqRef = useRef(0);

  const fetchPage = useCallback(
    async (mode: "reset" | "more") => {
      const seq = ++seqRef.current;
      setLoading(true);
      try {
        const page = await listFiles({
          limit: PAGE,
          cursor: mode === "more" ? cursor : null,
          kind: filters.kind || null,
          status: filters.status || null,
          doc_topic: filters.topic || null,
        });
        if (seq !== seqRef.current) return; // superseded — drop silently
        setItems((prev) =>
          mode === "reset" ? page.items : [...prev, ...page.items],
        );
        setCursor(page.next_cursor);
        setLoading(false);
      } catch (err) {
        if (seq !== seqRef.current) return; // superseded — the newer fetch reports errors
        toastError(err);
        setLoading(false);
      }
    },
    [cursor, filters],
  );

  // Reset (filters changed / refresh signal) — depends only on FILTERS, so a
  // cursor change doesn't re-trigger it.
  useEffect(() => {
    void fetchPage("reset");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, refreshSignal]);

  function changeFilter(key: keyof Filters, value: string): void {
    setFilters((f) => ({ ...f, [key]: value }));
  }

  async function view(file: FileOut): Promise<void> {
    try {
      const { url } = await getFileUrl(file.id, false);
      window.open(url, "_blank", "noopener");
    } catch (err) {
      toastError(err);
    }
  }

  async function download(file: FileOut): Promise<void> {
    try {
      const { url } = await getFileUrl(file.id, true);
      const a = document.createElement("a");
      a.href = url;
      a.rel = "noopener";
      a.click();
    } catch (err) {
      toastError(err);
    }
  }

  async function remove(file: FileOut): Promise<void> {
    if (!window.confirm(`Hard-delete "${file.original_name}"? Row + stored object both go away.`))
      return;
    try {
      await deleteFile(file.id);
      setItems((prev) => prev.filter((f) => f.id !== file.id));
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <section className="grid-section">
      <div className="grid-head">
        <h2>Files</h2>
        <div className="grid-filters">
          <select value={filters.kind} onChange={(e) => changeFilter("kind", e.target.value)}>
            <option value="">kind: all</option>
            <option value="image">image</option>
            <option value="pdf">pdf</option>
            <option value="document">document</option>
            <option value="text">text</option>
            <option value="other">other</option>
          </select>
          <select value={filters.status} onChange={(e) => changeFilter("status", e.target.value)}>
            <option value="">status: all</option>
            <option value="uploaded">uploaded</option>
            <option value="processing">processing</option>
            <option value="done">done</option>
            <option value="failed">failed</option>
          </select>
          <select value={filters.topic} onChange={(e) => changeFilter("topic", e.target.value)}>
            <option value="">topic: all</option>
            <option value="other">other</option>
          </select>
          <button className="btn ghost" onClick={() => void fetchPage("reset")}>
            Refresh
          </button>
        </div>
      </div>

      {items.length === 0 && !loading ? (
        <div className="grid-empty">No files match. Drop something above.</div>
      ) : (
        <div className="cards">
          {items.map((file) => (
            <Card
              key={file.id}
              file={file}
              onView={() => void view(file)}
              onDownload={() => void download(file)}
              onRename={async (name) => {
                try {
                  const updated = await renameFile(file.id, name);
                  setItems((prev) => prev.map((f) => (f.id === file.id ? updated : f)));
                } catch (err) {
                  toastError(err);
                }
              }}
              onDelete={() => void remove(file)}
            />
          ))}
        </div>
      )}

      {loading && <div className="grid-empty">loading…</div>}
      {cursor && !loading && (
        <div className="load-more">
          <button className="btn" onClick={() => void fetchPage("more")}>
            Load more
          </button>
        </div>
      )}
    </section>
  );
}

function Card({
  file,
  onView,
  onDownload,
  onRename,
  onDelete,
}: {
  file: FileOut;
  onView(): void;
  onDownload(): void;
  onRename(name: string): Promise<void>;
  onDelete(): void;
}): React.JSX.Element {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(file.original_name);

  return (
    <div className="card">
      <div className="card-top">
        <span className="card-kind" title={file.mime_type}>
          {file.kind ? (KIND_ICON[file.kind] ?? "📦") : "❔"}
        </span>
        <span className={STATUS_CLASS[file.status] ?? "chip"}>{file.status}</span>
      </div>

      {renaming ? (
        <form
          className="rename-form"
          onSubmit={async (e) => {
            e.preventDefault();
            if (name.trim() && name !== file.original_name) await onRename(name.trim());
            setRenaming(false);
          }}
        >
          <input
            autoFocus
            value={name}
            maxLength={255}
            onChange={(e) => setName(e.target.value)}
          />
          <button className="btn small primary">Save</button>
          <button
            type="button"
            className="btn small ghost"
            onClick={() => {
              setName(file.original_name);
              setRenaming(false);
            }}
          >
            ×
          </button>
        </form>
      ) : (
        <div
          className="card-name"
          title={`${file.original_name} — double-click to rename`}
          onDoubleClick={() => setRenaming(true)}
        >
          {file.original_name}
        </div>
      )}

      <div className="card-meta">
        <span>{fmtBytes(file.size_bytes)}</span>
        <span>{fmtDate(file.created_at)}</span>
        {file.sha256 && (
          <span className="mono" title={file.sha256}>
            #{file.sha256.slice(0, 8)}
          </span>
        )}
        {file.doc_topic && <span className="chip topic">{file.doc_topic}</span>}
      </div>

      <div className="card-actions">
        <button className="btn small" onClick={onView}>
          View
        </button>
        <button className="btn small" onClick={onDownload}>
          Download
        </button>
        <button className="btn small" onClick={() => setRenaming(true)}>
          Rename
        </button>
        <button className="btn small danger" onClick={onDelete}>
          Delete
        </button>
      </div>
    </div>
  );
}

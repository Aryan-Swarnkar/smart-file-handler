/** File browser: files grouped into FOLDERS by kind (Images / PDFs /
 * Documents / Text / Other — the organization is visible at a glance, no
 * filter fiddling), over the REAL keyset pagination (cursor + Load more).
 * Per-file actions: view (presigned URL), download (attachment disposition),
 * rename, hard delete. Kind/status/topic filters stay for power use.
 *
 * The View action opens its tab SYNCHRONOUSLY in the click handler: browsers
 * only honor window.open while transient user activation is alive, and any
 * await before the open (or a busy main thread) can let it expire → the
 * popup is blocked SILENTLY, which reads as "the View button does nothing".
 * The blank tab is navigated once the presigned URL arrives; a hard-blocked
 * popup falls back to an anchor click, so View can never fail silently.
 */

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { deleteFile, getFileUrl, listFiles, renameFile } from "../api/files";
import { ApiError } from "../api/client";
import type { FileKind, FileOut } from "../types";
import { fmtBytes, fmtDate } from "../types";
import { toastError } from "./Toaster";

const KIND_ICON: Record<string, string> = {
  image: "🖼️",
  pdf: "📕",
  document: "📄",
  text: "📝",
  other: "📦",
};

/** Folder sections, in display order. `null` kind (init rows that never
 * completed — transient, swept server-side) groups under "other". */
const FOLDERS: { key: FileKind; label: string; icon: string }[] = [
  { key: "image", label: "Images", icon: "🖼️" },
  { key: "pdf", label: "PDFs", icon: "📕" },
  { key: "document", label: "Documents", icon: "📄" },
  { key: "text", label: "Text files", icon: "📝" },
  { key: "other", label: "Other", icon: "📦" },
];

const STATUS_CLASS: Record<string, string> = {
  uploaded: "chip uploaded",
  processing: "chip processing",
  done: "chip done",
  failed: "chip failed",
};

const PAGE = 20;
/** Debounce for refreshSignal (one bump per completed upload): a 2000-file
 * drop used to trigger 2000 page-1 refetches; coalesce them. */
const REFRESH_DEBOUNCE_MS = 400;

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

interface FolderGroup {
  key: FileKind;
  label: string;
  icon: string;
  files: FileOut[];
}

export function FileGrid({ refreshSignal }: Props): React.JSX.Element {
  const [items, setItems] = useState<FileOut[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  // Folder collapse state: absent key = open (folders start expanded).
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
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

  // Reset on filter/refresh changes — debounced so a burst of upload
  // completions coalesces into one refetch (refreshSignal bumps per file).
  useEffect(() => {
    const handle = window.setTimeout(() => void fetchPage("reset"), REFRESH_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, refreshSignal]);

  function changeFilter(key: keyof Filters, value: string): void {
    setFilters((f) => ({ ...f, [key]: value }));
  }

  function toggleFolder(key: string): void {
    setCollapsed((prev) => ({ ...prev, [key]: !(prev[key] !== false) }));
  }

  /** View: open the tab FIRST (inside the click's user-activation window —
   * the ONLY reliable way; any await before window.open risks a silent
   * popup block), then navigate it when the presigned URL arrives. */
  function view(file: FileOut): void {
    const win = window.open("about:blank", "_blank");
    if (win) win.opener = null; // noopener semantics while still same-origin
    void getFileUrl(file.id, false)
      .then(({ url }) => {
        if (win) {
          win.location.href = url;
        } else {
          // Hard popup block: anchor fallback (no await in between, so the
          // activation is still alive) — never fail silently.
          const a = document.createElement("a");
          a.href = url;
          a.target = "_blank";
          a.rel = "noopener";
          a.click();
        }
      })
      .catch((err) => {
        try {
          win?.close();
        } catch {
          /* tab already navigated or closed */
        }
        toastError(err);
      });
  }

  /** Download: fetch the presigned bytes into a blob and hand them to the
   * browser via an object-URL anchor with the ORIGINAL filename.
   *
   * Why not a plain anchor to the presigned URL? Verified live: SeaweedFS
   * ignores the signed `response-content-disposition=attachment` override and
   * serves `inline`, so an anchor click NAVIGATES the app tab and Chrome
   * renders the file in place (PDF/text/image) instead of downloading —
   * "Download" made the whole UI vanish. A blob: anchor always downloads,
   * never navigates, and names the file correctly (the backend can only name
   * it after the storage-key uuid). Bucket CORS explicitly allows GET. */
  async function download(file: FileOut): Promise<void> {
    try {
      const { url } = await getFileUrl(file.id, true);
      const resp = await fetch(url);
      if (!resp.ok) {
        toastError(new ApiError(resp.status, "download_failed", `Storage answered HTTP ${resp.status}`));
        return;
      }
      const blob = await resp.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = file.original_name;
      a.rel = "noopener";
      a.click();
      // Revoke once the download has definitely started; leaving it around
      // for a few seconds is harmless, revoking too early can cancel it.
      window.setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
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

  const groups: FolderGroup[] = useMemo(() => {
    const byKind = new Map<string, FileOut[]>();
    for (const f of items) {
      const k = f.kind ?? "other";
      const arr = byKind.get(k);
      if (arr) arr.push(f);
      else byKind.set(k, [f]);
    }
    return FOLDERS.map((folder) => ({
      ...folder,
      files: byKind.get(folder.key) ?? [],
    })).filter((g) => g.files.length > 0);
  }, [items]);

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
        groups.map((g) => {
          const open = collapsed[g.key] !== false;
          return (
            <div className="folder" key={g.key}>
              <button
                className="folder-head"
                onClick={() => toggleFolder(g.key)}
                aria-expanded={open}
                title={open ? "Click to collapse" : "Click to expand"}
              >
                <span className="folder-icon">{g.icon}</span>
                <span className="folder-label">{g.label}</span>
                <span className="folder-count">{g.files.length}</span>
                <span className={`folder-chevron${open ? "" : " closed"}`}>▾</span>
              </button>
              {open && (
                <div className="cards">
                  {g.files.map((file) => (
                    <Card
                      key={file.id}
                      file={file}
                      onView={() => view(file)}
                      onDownload={() => void download(file)}
                      onRename={async (name) => {
                        try {
                          const updated = await renameFile(file.id, name);
                          setItems((prev) =>
                            prev.map((f) => (f.id === file.id ? updated : f)),
                          );
                        } catch (err) {
                          toastError(err);
                        }
                      }}
                      onDelete={() => void remove(file)}
                    />
                  ))}
                </div>
              )}
            </div>
          );
        })
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

const Card = memo(function Card({
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
});

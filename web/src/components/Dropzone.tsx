/** Dropzone: drag & drop files OR folders, plus picker buttons.
 *
 * Folder traversal detail: webkitGetAsEntry() must be called SYNCHRONOUSLY
 * inside the drop handler (the DataTransferItemList is neutered after the
 * event microtask), so entries are collected first, then walked async. */

import { useRef, useState } from "react";

interface Props {
  onFiles: (files: File[]) => void;
}

export function Dropzone({ onFiles }: Props): React.JSX.Element {
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const dirInput = useRef<HTMLInputElement>(null);

  function handleDrop(e: React.DragEvent): void {
    e.preventDefault();
    setDragging(false);
    const files: File[] = [];

    // 1. Synchronous: grab FileSystemEntry handles before the list dies.
    const entries: FileSystemEntry[] = [];
    const items = e.dataTransfer?.items;
    if (items) {
      for (let i = 0; i < items.length; i++) {
        const entry = items[i].webkitGetAsEntry?.();
        if (entry) entries.push(entry);
      }
    }

    // No real filesystem entries (synthetic drops, plain-file-only drops,
    // browsers without entries) → the flat file list is the payload.
    if (entries.length === 0) {
      files.push(...Array.from(e.dataTransfer?.files ?? []));
    }

    // 2. Async: walk entries (files directly, directories recursively).
    const walk = async (entry: FileSystemEntry): Promise<void> => {
      if (entry.isFile) {
        const file = await new Promise<File | null>((resolve) =>
          (entry as FileSystemFileEntry).file(resolve, () => resolve(null)),
        );
        if (file) files.push(file);
      } else if (entry.isDirectory) {
        const reader = (entry as FileSystemDirectoryEntry).createReader();
        // readEntries returns in batches — loop until a batch comes back empty.
        for (;;) {
          const batch = await new Promise<FileSystemEntry[]>((resolve) =>
            reader.readEntries(resolve, () => resolve([])),
          );
          if (batch.length === 0) break;
          for (const child of batch) await walk(child);
        }
      }
    };

    void (async () => {
      await Promise.all(entries.map(walk));
      if (files.length > 0) onFiles(files);
    })();
  }

  function collect(input: HTMLInputElement | null): void {
    if (!input?.files) return;
    const files = Array.from(input.files);
    if (files.length > 0) onFiles(files);
    input.value = ""; // allow re-adding the same file
  }

  return (
    <div
      className={`dropzone ${dragging ? "active" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
    >
      <div className="dz-icon">⬆️</div>
      <div className="dz-title">Drop files or folders here</div>
      <div className="dz-sub">presigned PUT straight to SeaweedFS — bytes never touch the API</div>
      <div className="dz-buttons">
        <button className="btn" onClick={() => fileInput.current?.click()}>
          Choose files
        </button>
        <button className="btn" onClick={() => dirInput.current?.click()}>
          Choose folder
        </button>
      </div>
      <input
        ref={fileInput}
        type="file"
        multiple
        hidden
        onChange={() => collect(fileInput.current)}
      />
      <input
        ref={dirInput}
        type="file"
        multiple
        hidden
        // Non-standard but supported everywhere that matters for a dev tool.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        {...({ webkitdirectory: "", directory: "" } as any)}
        onChange={() => collect(dirInput.current)}
      />
    </div>
  );
}

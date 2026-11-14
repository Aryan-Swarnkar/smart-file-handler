/** XHR PUT to a presigned URL — XHR (not fetch) because it's the only browser
 * primitive with UPLOAD progress events.
 *
 * Contract notes from the backend:
 * - the URL's SigV4 signature IS the authorization: no headers needed at all;
 * - SigV4 presigned URLs sign only the Host header, so any Content-Type the
 *   browser picks is fine (verified live in P3);
 * - aborting AFTER the PUT but BEFORE /complete leaves an orphan init row —
 *   by design, cleaned by the P4 worker sweep. */

import { ApiError } from "./client";

export interface UploadHandle {
  promise: Promise<void>;
  abort(): void;
}

export function putToPresigned(
  url: string,
  file: File,
  onProgress: (fraction: number) => void,
): UploadHandle {
  const xhr = new XMLHttpRequest();

  const promise = new Promise<void>((resolve, reject) => {
    xhr.open("PUT", url);

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    });

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
      } else {
        reject(
          new ApiError(
            xhr.status,
            "s3_put_failed",
            `Storage rejected the upload (HTTP ${xhr.status}). Presigned URL may have expired — retry.`,
          ),
        );
      }
    });

    xhr.addEventListener("error", () =>
      reject(
        new ApiError(0, "network_error", "Upload to storage failed (network or CORS)."),
      ),
    );

    xhr.addEventListener("abort", () =>
      reject(new DOMException("Upload canceled", "AbortError")),
    );

    xhr.send(file);
  });

  return {
    promise,
    abort: () => xhr.abort(),
  };
}

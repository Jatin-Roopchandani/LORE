// Uploading a book from the browser: POST /api/books/upload as multipart form data, with the bytes' progress.
// XMLHttpRequest, because fetch cannot report upload progress. Kept out of api.ts so tests that mock api still work.
import { csrfHeaders } from "./csrf";
import { isMulti } from "./routing";
import { reportKeyProblem } from "./keyProblem";
import type { UploadError, UploadResult } from "./types";

export const UPLOAD_URL = "/api/books/upload";
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;
export interface UploadProgress { loaded: number; total: number }

// Where the browser goes when the session is over (multi-user mode). A seam for tests.
export const uploadNav = { to: (path: string) => window.location.assign(path) };

const fail = (code: string, message = ""): UploadError => ({ error: { code, message } });

export function uploadBook(file: File, world: string, onProgress: (p: UploadProgress) => void,
  signal?: AbortSignal): Promise<UploadResult | UploadError> {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", UPLOAD_URL);
    xhr.setRequestHeader("Accept", "application/json");
    for (const [name, value] of Object.entries(csrfHeaders())) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress({ loaded: e.loaded, total: e.total }); };
    xhr.onload = () => {
      if (xhr.status === 401 && isMulti()) { uploadNav.to("/signin"); resolve(fail("unauthorized")); return; }
      let body: unknown = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* not JSON: a proxy's page */ }
      const err = (body as UploadError | null)?.error;
      if (err && typeof err === "object" && typeof err.code === "string") {
        reportKeyProblem(err.code);
        resolve({ error: { code: err.code, message: String(err.message ?? "") }, book_id: (body as UploadError).book_id });
        return;
      }
      if (xhr.status === 202 && body && typeof (body as UploadResult).book_id === "string") { resolve(body as UploadResult); return; }
      if (xhr.status === 413) { resolve(fail("too_large")); return; }
      if (xhr.status === 403) { resolve(fail("forbidden")); return; }
      resolve(fail(`http_${xhr.status}`));
    };
    xhr.onerror = () => resolve(fail("unreachable"));
    xhr.onabort = () => resolve(fail("aborted"));
    if (signal) {
      if (signal.aborted) { resolve(fail("aborted")); return; }
      signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }
    const form = new FormData();
    form.append("world", world);
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

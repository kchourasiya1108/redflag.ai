import type {
  Backtest, CaseFile, Explain, IpoDetail, IpoRow, RunState, RunSummary, Status, TraceEvent, Upload,
  UploadFields,
} from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function get<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, init);
  if (!resp.ok) {
    let message = `${resp.status} ${resp.statusText}`;
    try {
      const body = await resp.json();
      message = typeof body.detail === "string" ? body.detail : body.error?.message ?? message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(resp.status, message);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  ipos: (q: string, limit = 50, recorded = false) =>
    get<{ total: number; items: IpoRow[] }>(
      `/api/ipos?q=${encodeURIComponent(q)}&limit=${limit}${recorded ? "&recorded=true" : ""}`),
  ipo: (id: string) => get<IpoDetail>(`/api/ipos/${encodeURIComponent(id)}`),
  page: (id: string, page: number) =>
    get<{ page: number; pages: number; text: string }>(`/api/ipos/${encodeURIComponent(id)}/pages/${page}`),
  sebi: (id: string) =>
    get<Record<string, string> & { named: Record<string, string>[] }>(`/api/sebi/${encodeURIComponent(id)}`),
  runs: () => get<{ active: RunSummary[]; runs: RunSummary[] }>("/api/runs"),
  run: (id: string) => get<RunState>(`/api/runs/${id}`),
  case: (id: string) => get<CaseFile>(`/api/runs/${id}/case`),
  explain: (id: string) => get<Explain>(`/api/runs/${id}/explain`),
  trace: (id: string) => get<TraceEvent[]>(`/api/runs/${id}/trace`),
  start: (ipo_id: string, sample_mode: boolean, auto = false) =>
    get<{ run_id: string }>("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ipo_id, sample_mode, auto }),
    }),
  /** Live when Zetaris and Nightshift answer, otherwise an offline check. */
  check: (ipo_id: string) => api.start(ipo_id, false, true),
  backtest: () => get<Backtest>("/api/backtest"),
  uploads: () => get<{ items: Upload[] }>("/api/uploads"),
  upcoming: () => get<{ items: Upload[]; last: { refreshed_at?: number; errors?: string[] }; refreshing: boolean; error: string | null }>("/api/upcoming"),
  refreshUpcoming: () => get<{ refreshing: boolean }>("/api/upcoming/refresh", { method: "POST" }),
  upload: (id: string) => get<Upload>(`/api/uploads/${encodeURIComponent(id)}`),
  editUpload: (id: string, changes: UploadFields) =>
    get<Upload>(`/api/uploads/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }),
  status: () => get<Status>("/api/status"),
};

/** Upload an RHP PDF with progress (fetch has no upload progress, so XHR). */
export function uploadRhp(file: File, onProgress: (fraction: number) => void): Promise<Upload> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.setRequestHeader("Content-Type", "application/pdf");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let body: { detail?: string } & Partial<Upload> = {};
      try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as Upload);
      else reject(new ApiError(xhr.status, typeof body.detail === "string" ? body.detail : `${xhr.status} ${xhr.statusText}`));
    };
    xhr.onerror = () => reject(new ApiError(0, "The upload failed. Check your connection and try again."));
    xhr.send(file);
  });
}

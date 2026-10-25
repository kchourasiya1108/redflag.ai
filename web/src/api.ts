import type {
  Backtest, CaseFile, Explain, IpoDetail, IpoRow, RunState, RunSummary, Status, TraceEvent,
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
  start: (ipo_id: string, sample_mode: boolean) =>
    get<{ run_id: string }>("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ipo_id, sample_mode }),
    }),
  backtest: () => get<Backtest>("/api/backtest"),
  status: () => get<Status>("/api/status"),
};

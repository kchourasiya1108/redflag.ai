// Shapes returned by app/api.py and agent/main.py (investigate).

export type Severity = "HIGH" | "ELEVATED" | "LOW";
export type Rating = Severity | "INSUFFICIENT DATA";

export interface IpoRow {
  ipo_id: string;
  company: string;
  symbol: string;
  exchange: string;
  open_date: string;
  issue_price: number | null;
  banker_raw: string | null;
  industry: string | null;
  has_rhp: boolean;
  ret_6m: number | null;
  adverse: number | null;
  in_sample: boolean;
  recorded: boolean;
}

export interface UploadFields {
  company?: string;
  open_date?: string;
  issue_price?: number;
  exchange?: string;
  lead_managers?: string[];
  registrar?: string;
  market_maker?: string;
  auditor?: string;
  lot_size?: number;
}

export interface Upload {
  upload_id: string;
  filename: string;
  sha256: string;
  pages: number;
  text_pages: number;
  status: "ok" | "scanned";
  uploaded_at: number;
  detected: UploadFields & { price_source?: string | null; cin?: string | null; issue_type?: string | null };
  fields: UploadFields;
  source?: { exchange?: string; symbol?: string; status?: string; open_date?: string | null;
    close_date?: string | null; price_band?: string | null; url?: string; rhp_url?: string };
  missing: string[];
  required: string[];
  exchanges: string[];
}

export interface IpoDetail {
  uploaded?: boolean;
  upload?: Upload;
  ipo: Record<string, string | number | null>;
  docs: { doc_type: string; pages: number; text_pages: number; status: string; url: string }[];
  in_sample: boolean;
  recorded: boolean;
  outcome: Record<string, string | number | null> | null;
  runs: RunSummary[];
}

export interface RunSummary {
  run_id: string;
  ipo_id: string;
  company: string | null;
  status: string;
  rating?: Rating;
  p_adverse?: number | null;
  depth?: string;
  data_source?: string;
  sample_mode: boolean;
  execution_time_seconds?: number;
  finished_at?: number;
  started_at?: number;
  recorded?: boolean;
  error?: RunErrorInfo | null;
}

export interface RunErrorInfo {
  type: string;
  message: string;
  recoverable: boolean;
}

export interface Reason {
  flag: string;
  title: string;
  severity: Severity;
  text: string;
  evidence: string[];
  confidence: number | null;
}

export interface SkepticNote {
  flag: string;
  ruling: "upheld" | "dismissed" | "downgraded" | string;
  reason: string;
  evidence: string[];
}

export interface Cleared {
  flag: string;
  title: string;
  text: string;
  evidence: string[];
}

export interface RunOutput {
  ipo: Record<string, string>;
  as_of: string;
  rating: Rating;
  p_adverse: number | null;
  model_version: string;
  depth: string;
  reasons: Reason[];
  skeptic_notes: SkepticNote[];
  cleared_checks: Cleared[];
  questions: string[];
  not_checked: string[];
  data_source: string;
  disclaimer: string;
  summary: string | null;
}

export interface RunResult {
  run_id: string;
  status: "success" | "partial";
  output: RunOutput;
  agents: { name: string; role: string; uses_llm: boolean }[];
  trace_id: string;
  execution_time_seconds: number;
}

export interface RunState {
  run_id: string;
  status: "running" | "success" | "partial" | "error";
  ipo_id?: string;
  sample_mode?: boolean;
  started_at?: number;
  error?: RunErrorInfo | null;
  result: RunResult | null;
}

export interface TraceEvent {
  timestamp: string;
  run_id: string;
  agent_name: string;
  action: string;
  input_summary: string;
  output_summary: string;
  target_agent: string | null;
  confidence: number | null;
  retry_count: number;
  status: string;
  detail?: Record<string, unknown>;
}

export interface Fact {
  field: string;
  fy: string;
  value: number;
  unit: string;
  page: number | null;
  verified: boolean;
  method: string;
  source: string;
  agent: string;
}

export interface CaseFile {
  meta: Record<string, unknown> & { depth_history?: { from: string | null; to: string; reason: string }[] };
  entities: { id: string; type: string; name: string; source: string; agent: string }[];
  edges: { src: string; dst: string; type: string; source: string; agent: string }[];
  facts: Fact[];
  verification: { statement: string; fy: string; passed: boolean; attempts: number; detail: string }[];
  flags: { id: string; severity: Severity; evidence: string[]; confidence: number; text: string; agent: string; value: unknown }[];
  score: { band: string; p_adverse: number | null; version: string; features: Record<string, number | null> };
}

export interface Explain {
  intercept: number;
  bands: { low_below: number; high_at: number };
  version: string;
  p_adverse: number | null;
  features: { feature: string; value: number | null; missing: boolean; contribution: number }[];
}

export interface Metrics {
  n: number;
  adverse: number;
  auc: number;
  recall_at_high: number;
  false_alarm_rate: number;
  brier: number;
}

export interface Backtest {
  results: {
    version: string;
    trained_at: string;
    split: { train: number; test: number; train_from: string; train_to: string; test_from: string; test_to: string };
    base_rate_train: number;
    selection: { validation_auc: Record<string, number>; validation: { n: number; from: string } };
    model: Metrics;
    baseline_banker_model_v0: Metrics;
    baseline_banker_rule: Metrics;
    coverage_test: Record<string, number>;
  } | null;
  llm_baseline: (Partial<Metrics> & Record<string, unknown>) | null;
  scorer?: { version: string; bands: { low_below: number; high_at: number }; features: string[] };
}

export interface Status {
  sample_mode_default: boolean;
  archive: boolean;
  scorer: boolean;
  zetaris: { configured: boolean; ok?: boolean; latency_ms?: number; error?: string };
  meterless: {
    configured: boolean;
    ok?: boolean;
    latency_ms?: number;
    error?: string;
    models?: number;
    nemotron?: string[];
    roles?: Record<string, string>;
  };
}

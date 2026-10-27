import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, Database, ExternalLink, FileText, Gavel, X } from "lucide-react";
import { api } from "../api";
import type { TraceEvent } from "../types";
import { date, useAsync } from "../lib";
import { ErrorBox, Skeleton } from "./ui";

/** Evidence refs: rhp:p45 (an RHP page), sebi:<order>, zq:<query hash>. */
interface Ctx {
  open: (ref: string, highlight?: string) => void;
}
const EvidenceCtx = createContext<Ctx>({ open: () => {} });

export function EvidenceProvider({ ipoId, trace, children }: {
  ipoId: string; trace: TraceEvent[]; children: ReactNode;
}) {
  const [current, setCurrent] = useState<{ ref: string; highlight?: string } | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setCurrent(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <EvidenceCtx.Provider value={{ open: (ref, highlight) => setCurrent({ ref, highlight }) }}>
      {children}
      {current && (
        <>
          <div className="scrim" onClick={() => setCurrent(null)} />
          <aside className="drawer" role="dialog" aria-label={`Evidence ${current.ref}`}>
            <Viewer ipoId={ipoId} trace={trace} refId={current.ref} highlight={current.highlight}
              onNavigate={(ref) => setCurrent({ ref, highlight: current.highlight })} onClose={() => setCurrent(null)} />
          </aside>
        </>
      )}
    </EvidenceCtx.Provider>
  );
}

export function EvidenceChip({ refId, highlight }: { refId: string; highlight?: string }) {
  const { open } = useContext(EvidenceCtx);
  const [kind, rest] = splitRef(refId);
  const Icon = kind === "rhp" ? FileText : kind === "sebi" ? Gavel : Database;
  const label = kind === "rhp" ? `Prospectus ${rest.replace(/^p/, "p. ")}` : kind === "sebi" ? "SEBI order" : "Data record";
  const hint = kind === "rhp" ? "Open this page of the prospectus" : kind === "sebi" ? `Open SEBI order ${rest}` : "See the data lookup behind this";
  return (
    <button className="chip" onClick={() => open(refId, highlight)} title={hint}>
      <Icon size={12} aria-hidden /> {label}
    </button>
  );
}

function splitRef(ref: string): [string, string] {
  const i = ref.indexOf(":");
  return i < 0 ? ["", ref] : [ref.slice(0, i), ref.slice(i + 1)];
}

function Viewer({ ipoId, trace, refId, highlight, onNavigate, onClose }: {
  ipoId: string; trace: TraceEvent[]; refId: string; highlight?: string;
  onNavigate: (ref: string) => void; onClose: () => void;
}) {
  const [kind, rest] = splitRef(refId);
  const title = kind === "rhp" ? `Prospectus, page ${rest.slice(1)}` : kind === "sebi" ? "SEBI order" : "Data lookup";
  const sub = kind === "rhp" ? "Red herring prospectus (RHP) filed with the exchange" : kind === "sebi" ? `Order ${rest}` : "A record from the IPO archive";
  const Icon = kind === "rhp" ? FileText : kind === "sebi" ? Gavel : Database;
  return (
    <>
      <div className="drawer-head">
        <Icon size={18} aria-hidden />
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 style={{ fontSize: 16 }}>{title}</h2>
          <div className="tiny faint">{sub}</div>
        </div>
        <button className="icon-btn" onClick={onClose} aria-label="Close"><X size={18} /></button>
      </div>
      <div className="drawer-body">
        {kind === "rhp" && <RhpPage ipoId={ipoId} page={Number(rest.slice(1))} highlight={highlight} onNavigate={onNavigate} />}
        {kind === "sebi" && <SebiOrder id={rest} />}
        {kind === "zq" && <QueryEvidence refId={refId} trace={trace} />}
        {!["rhp", "sebi", "zq"].includes(kind) && <p className="muted">This reference ({refId}) has no viewer.</p>}
      </div>
    </>
  );
}

/** Numbers in the claim, so the page can highlight where they appear. */
function numbersIn(text?: string): string[] {
  if (!text) return [];
  const found = text.match(/\d[\d,]*\.\d+|\d{2,}[\d,]*/g) ?? [];
  return [...new Set(found.map((n) => n.replace(/,/g, "")))].filter((n) => n.length >= 2);
}

function RhpPage({ ipoId, page, highlight, onNavigate }: {
  ipoId: string; page: number; highlight?: string; onNavigate: (ref: string) => void;
}) {
  const { data, error, loading } = useAsync(() => api.page(ipoId, page), [ipoId, page]);
  const nums = useMemo(() => numbersIn(highlight), [highlight]);
  const parts = useMemo(() => {
    if (!data) return [];
    if (!nums.length) return [data.text];
    // Match a number in the page however it is grouped (1,436.51 or 1436.51).
    const pattern = nums
      .sort((a, b) => b.length - a.length)
      .map((n) => n.split("").map((c) => (c === "." ? "\\." : c)).join(",?"))
      .join("|");
    // Whole numbers only: 9.8 must not light up inside 9.82.
    return data.text.split(new RegExp(`(?<![\\d.])(${pattern})(?![\\d])`, "g"));
  }, [data, nums]);
  const textRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    textRef.current?.querySelector("mark")?.scrollIntoView({ block: "center" });
  }, [parts]);
  if (loading) return <div className="stack-sm">{[...Array(12)].map((_, i) => <Skeleton key={i} h={14} w={`${60 + ((i * 37) % 40)}%`} />)}</div>;
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;
  return (
    <div className="stack">
      <div className="between">
        <button className="btn btn-sm" disabled={page <= 1} onClick={() => onNavigate(`rhp:p${page - 1}`)}>
          <ChevronLeft size={14} /> Page {page - 1}
        </button>
        <span className="small faint">page {page} of {data.pages}</span>
        <button className="btn btn-sm" disabled={page >= data.pages} onClick={() => onNavigate(`rhp:p${page + 1}`)}>
          Page {page + 1} <ChevronRight size={14} />
        </button>
      </div>
      {nums.length > 0 && (
        <p className="small muted">Highlighted: numbers from the finding that appear on this page. Prospectuses often print amounts in ₹ lakh, so a crore figure may not appear exactly as written.</p>
      )}
      <div className="page-text" ref={textRef}>
        {parts.map((p, i) => (i % 2 === 1 ? <mark key={i}>{p}</mark> : <span key={i}>{p}</span>))}
      </div>
    </div>
  );
}

function SebiOrder({ id }: { id: string }) {
  const { data, error, loading } = useAsync(() => api.sebi(id), [id]);
  if (loading) return <Skeleton h={120} />;
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;
  return (
    <div className="stack">
      <div className="stack-sm">
        <div className="row-wrap">
          <span className="pill">{data.order_type}</span>
          <span className="pill">{date(data.order_date)}</span>
        </div>
        <h3 style={{ fontSize: 16 }}>{data.title}</h3>
      </div>
      <div className="row-wrap">
        {data.url && <a className="btn btn-sm" href={data.url} target="_blank" rel="noreferrer">Open on sebi.gov.in <ExternalLink size={13} /></a>}
        {data.pdf_url && <a className="btn btn-sm" href={data.pdf_url} target="_blank" rel="noreferrer">Order PDF <ExternalLink size={13} /></a>}
      </div>
      {data.named.length > 0 && (
        <div className="stack-sm">
          <div className="eyebrow">Named in this order</div>
          <div className="table-wrap card">
            <table>
              <thead><tr><th>Entity</th><th>Role</th><th>Action</th></tr></thead>
              <tbody>
                {data.named.map((n, i) => (
                  <tr key={i}><td>{n.entity_name}</td><td className="muted">{n.entity_role || "–"}</td><td className="muted">{n.action || n.finding || "–"}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      <p className="small muted">Being named in an order is not a finding against the company being rated. We only link orders dated before the bid opened.</p>
    </div>
  );
}

function QueryEvidence({ refId, trace }: { refId: string; trace: TraceEvent[] }) {
  const ev = trace.find((e) => e.action === "data.query" && e.detail?.ref === refId);
  if (!ev) return <p className="muted">This lookup is not in the check's record.</p>;
  const [template, rows] = ev.output_summary.split(" -> ");
  return (
    <div className="stack">
      <div className="kv">
        <div><div className="k">What we looked up</div><div className="v">{lookupName(template)}</div></div>
        <div><div className="k">Records found</div><div className="v">{rows?.replace(" rows", "") ?? "–"}</div></div>
        <div><div className="k">Later records hidden</div><div className="v">{String(ev.detail?.guard_dropped ?? 0)}</div></div>
      </div>
      <p className="small muted">Every lookup only sees records dated before the bid opened, so a past IPO is judged on what an investor could have known at the time.</p>
      <details className="disclosure">
        <summary>Technical details</summary>
        <div className="stack-sm" style={{ marginTop: 10 }}>
          <div className="small"><span className="faint">Query template</span> <span className="mono">{template}</span></div>
          <div className="small"><span className="faint">Served by</span> {String(ev.detail?.source ?? "–")}</div>
          <div className="page-text">{ev.input_summary}</div>
        </div>
      </details>
    </div>
  );
}

/** banker_history -> "Banker history". */
function lookupName(template: string): string {
  const t = template.replace(/[_.]/g, " ").trim();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

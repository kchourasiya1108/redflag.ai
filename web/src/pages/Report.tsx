import { useMemo, useState, type ReactNode } from "react";
import {
  CheckCircle2, ChevronLeft, CircleSlash, Download, FileJson, HelpCircle, MessageSquareQuote, RotateCcw,
  Scale, ShieldAlert, Sparkles, XCircle,
} from "lucide-react";
import { api } from "../api";
import { AgentTimeline, Contributions, PartyGraph, ProbabilityScale } from "../components/Charts";
import { EvidenceChip, EvidenceProvider } from "../components/Evidence";
import { Card, Empty, SeverityBadge } from "../components/ui";
import type { CaseFile, Explain, Fact, Reason, RunResult, SkepticNote, TraceEvent } from "../types";
import { FIELD_NAMES, FLAGS, Link, STATEMENTS, cx, date, navigate, num, pct, seconds } from "../lib";
import { useRunStats } from "./Run";

type Tab = "findings" | "financials" | "score" | "parties" | "trace";

export function Report({ result, cf, explain, trace }: {
  result: RunResult; cf: CaseFile | null; explain: Explain | null; trace: TraceEvent[];
}) {
  const o = result.output;
  const [tab, setTab] = useState<Tab>("findings");
  const stats = useRunStats(trace);
  const sample = o.data_source.startsWith("sample");
  const bands = explain?.bands ?? { low_below: 0.1154, high_at: 0.2308 };
  const depthWhy = cf?.meta.depth_history?.map((d) => d.reason).filter(Boolean).join("; then ");
  const factCount = cf?.facts.filter((f) => f.verified).length ?? 0;

  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "findings", label: "Findings", count: o.reasons.length },
    { id: "financials", label: "Financials", count: factCount || undefined },
    { id: "score", label: "Why this score" },
    { id: "parties", label: "Parties", count: cf?.entities.length },
    { id: "trace", label: "Trace", count: trace.length || undefined },
  ];

  return (
    <EvidenceProvider ipoId={o.ipo.ipo_id} trace={trace}>
      <div className="container stack-lg">
        <div className="stack-sm">
          <Link to={`/ipo/${o.ipo.ipo_id}`} className="small row"><ChevronLeft size={14} /> {o.ipo.company}</Link>
          <div className="between" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
            <div className="stack-sm">
              <div className="row-wrap">
                <span className="pill">{sample ? "Sample mode (offline replay)" : "Live · Zetaris + Nightshift"}</span>
                <span className="pill">as of {date(o.as_of)}</span>
                <span className="pill">{o.depth}</span>
                <span className="pill">{seconds(result.execution_time_seconds)}</span>
                {result.status === "partial" && <span className="badge sev-ELEVATED">Partial: time limit reached</span>}
              </div>
              <h1>{o.ipo.company}</h1>
              <div className="small faint">{o.ipo.exchange} · <span className="mono">{o.ipo.symbol}</span> · issue price ₹{o.ipo.issue_price} · lead manager {o.ipo.banker_raw}</div>
            </div>
            <div className="row-wrap">
              <a className="btn btn-sm" href={`/api/runs/${result.run_id}/report.md`} download={`${result.run_id}-report.md`}><Download size={14} /> Report</a>
              <a className="btn btn-sm" href={`/api/runs/${result.run_id}/case`} download={`${result.run_id}-case_file.json`}><FileJson size={14} /> Case file</a>
              <RerunButton ipoId={o.ipo.ipo_id} sample={sample} />
            </div>
          </div>
        </div>

        <Card>
          <div className="verdict">
            <div className="stack">
              <div className="row-wrap" style={{ gap: 14 }}>
                <SeverityBadge level={o.rating} size="xl" />
                <div>
                  <div className="eyebrow">Probability of an adverse outcome</div>
                  <div className="prob-big">{pct(o.p_adverse)}</div>
                </div>
              </div>
              <div className="small muted" style={{ maxWidth: 560 }}>
                Adverse means the stock fell hard in its first six months, was suspended, or the company drew SEBI action.
                The base rate in training was {pct(bands.low_below)}, which is also where LOW ends. {depthWhy && <>Depth {o.depth}: {depthWhy}.</>}
              </div>
            </div>
            <div style={{ minWidth: 300, maxWidth: 460, width: "100%" }}>
              <ProbabilityScale p={o.p_adverse} bands={bands} />
            </div>
          </div>
        </Card>

        <div className="report-grid">
          <div className="stack">
            <SummaryCard summary={o.summary} />
            <div className="tabs" role="tablist">
              {tabs.map((t) => (
                <button key={t.id} role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}>
                  {t.label} {t.count !== undefined && <span className="count">{t.count}</span>}
                </button>
              ))}
            </div>
            {tab === "findings" && <Findings result={result} />}
            {tab === "financials" && (cf ? <Financials cf={cf} /> : <Empty title="No case file" />)}
            {tab === "score" && (explain ? (
              <Card title="How each signal moved the probability" hint="The Scorer's own arithmetic: a logistic model on standardised features">
                <Contributions explain={explain} />
              </Card>
            ) : <Empty title="No score breakdown for this run" />)}
            {tab === "parties" && (cf ? (
              <Card title="Parties to the issue" hint="Red: named in a SEBI order dated before the bid">
                {cf.entities.length ? <PartyGraph cf={cf} company={o.ipo.company} /> : <Empty title="No parties recorded" />}
              </Card>
            ) : <Empty title="No case file" />)}
            {tab === "trace" && <TraceTab trace={trace} />}
          </div>

          <aside className="stack">
            {o.questions.length > 0 && (
              <Card title={<span className="row"><HelpCircle size={16} /> Questions to ask</span>}>
                <ul className="stack-sm small" style={{ margin: 0, paddingLeft: 18 }}>
                  {o.questions.map((q, i) => <li key={i}>{q}</li>)}
                </ul>
              </Card>
            )}
            <Card title="This run">
              <div className="stack-sm small">
                <Row k="Run" v={<span className="mono">{result.run_id}</span>} />
                <Row k="Data" v={o.data_source} />
                <Row k="Model" v={<span className="mono">{o.model_version}</span>} />
                <Row k="Zetaris queries" v={stats.queries.length} />
                <Row k="Nemotron calls" v={`${stats.calls.length} · ${(stats.tokens / 1000).toFixed(1)}k tokens`} />
                {stats.models.map((m) => <Row key={m} k="" v={<span className="mono tiny">{m.replace("nvidia/", "")}</span>} />)}
                <Row k="Engines" v={`Markovian ${stats.markovian} · H-MEM ${stats.hmem} · World Model`} />
                <Row k="Agents" v={result.agents.length} />
              </div>
            </Card>
            <details className="card">
              <summary className="card-head" style={{ cursor: "pointer", borderBottom: 0 }}><h2 style={{ fontSize: 15.5 }}>Agents in this run ({result.agents.length})</h2></summary>
              <div className="stack-sm" style={{ padding: "0 20px 16px" }}>
                {result.agents.map((a) => (
                  <div key={a.name} className="small">
                    <div className="row" style={{ gap: 6 }}><b>{a.name}</b>{a.uses_llm && <span className="pill" style={{ height: 20, fontSize: 11 }}><Sparkles size={11} /> Nemotron</span>}</div>
                    <div className="faint tiny">{a.role}</div>
                  </div>
                ))}
              </div>
            </details>
            <p className="tiny faint">{o.disclaimer}</p>
          </aside>
        </div>
      </div>
    </EvidenceProvider>
  );
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return <div className="between" style={{ alignItems: "baseline" }}><span className="faint">{k}</span><span style={{ textAlign: "right" }}>{v}</span></div>;
}

function RerunButton({ ipoId, sample }: { ipoId: string; sample: boolean }) {
  const [busy, setBusy] = useState(false);
  return (
    <button className="btn btn-sm" disabled={busy} onClick={async () => {
      setBusy(true);
      try {
        navigate(`/run/${(await api.start(ipoId, sample)).run_id}`);
      } catch (e) {
        alert((e as Error).message);
      }
      setBusy(false);
    }}>
      <RotateCcw size={14} /> Run again
    </button>
  );
}

function SummaryCard({ summary }: { summary: string | null }) {
  if (!summary) {
    return (
      <div className="callout">
        <MessageSquareQuote size={18} />
        <div>
          <b>No written summary for this run.</b> Nemotron's draft is published only if every number in it is in the case file
          and it cites no flag the Skeptic dismissed; this draft did not pass, so the findings below stand on their own.
        </div>
      </div>
    );
  }
  return (
    <Card>
      <div className="stack-sm">
        <div className="row small faint"><Sparkles size={14} /> Summary by Nemotron · every number checked against the case file</div>
        <p className="summary">{summary}</p>
      </div>
    </Card>
  );
}

// -- findings -----------------------------------------------------------------

function Findings({ result }: { result: RunResult }) {
  const o = result.output;
  const note = (flag: string) => o.skeptic_notes.find((n) => n.flag === flag);
  const dismissed = o.skeptic_notes.filter((n) => n.ruling === "dismissed");
  const dismissedIds = new Set(dismissed.map((d) => d.flag));
  const cleared = o.cleared_checks.filter((c) => !dismissedIds.has(c.flag));
  return (
    <div className="stack-lg">
      <section className="stack-sm">
        <div className="section-title" style={{ marginBottom: 4 }}>
          <h2>Red flags</h2>
          <span className="hint">Click a reference to see the page or query behind it</span>
        </div>
        {o.reasons.length === 0 && <div className="card"><Empty icon={<CheckCircle2 color="var(--low)" />} title="No flag held up">Every check either passed or was explained away by the RHP itself.</Empty></div>}
        {o.reasons.map((r) => <FlagCard key={r.flag} reason={r} note={note(r.flag)} />)}
      </section>

      {dismissed.length > 0 && (
        <section className="stack-sm">
          <div className="section-title" style={{ marginBottom: 4 }}>
            <h2>Dismissed by the Skeptic</h2>
            <span className="hint">Raised, then explained by the RHP</span>
          </div>
          {dismissed.map((d) => {
            const c = o.cleared_checks.find((x) => x.flag === d.flag);
            return (
              <FlagCard key={d.flag} dismissed note={d}
                reason={{ flag: d.flag, title: c?.title ?? FLAGS[d.flag]?.title ?? d.flag, severity: "LOW", text: c?.text ?? "", evidence: c?.evidence ?? [], confidence: null }} />
            );
          })}
        </section>
      )}

      <div className="grid-2">
        <Card title={<span className="row"><CheckCircle2 size={16} className="ok-ic" /> Checks that passed</span>}>
          {cleared.length === 0 && <p className="small muted">None.</p>}
          {cleared.map((c) => (
            <div className="check-row" key={c.flag}>
              <span className="ic ok-ic"><CheckCircle2 size={16} /></span>
              <div className="stack-sm" style={{ gap: 4 }}>
                <div className="small"><span className="mono faint">{c.flag}</span> <b>{c.title}</b></div>
                <div className="small muted">{c.text}</div>
                <div className="row-wrap">{c.evidence.slice(0, 4).map((e) => <EvidenceChip key={e} refId={e} highlight={c.text} />)}</div>
              </div>
            </div>
          ))}
        </Card>
        <Card title={<span className="row"><CircleSlash size={16} className="off-ic" /> Not checked</span>} hint="Said plainly, never silently skipped">
          {o.not_checked.map((n, i) => {
            const at = n.indexOf(": ");
            return (
              <div className="check-row" key={i}>
                <span className="ic off-ic"><CircleSlash size={15} /></span>
                <div className="small"><b>{notCheckedTitle(at > 0 ? n.slice(0, at) : n)}</b>{at > 0 && <div className="muted">{n.slice(at + 2)}</div>}</div>
              </div>
            );
          })}
        </Card>
      </div>
    </div>
  );
}

/** "F5" alone reads better as "F5 Customer concentration". */
function notCheckedTitle(item: string): string {
  return /^F\d{1,2}$/.test(item) && FLAGS[item] ? `${item} ${FLAGS[item].title}` : item;
}

function FlagCard({ reason, note, dismissed }: { reason: Reason; note?: SkepticNote; dismissed?: boolean }) {
  const meta = FLAGS[reason.flag];
  return (
    <article className={cx("flag-card", dismissed && "dismissed")}>
      <div className="top">
        <span className="id">{reason.flag}</span>
        <div style={{ minWidth: 0 }}>
          <div className="title">{reason.title}</div>
          {meta && <div className="tiny faint">{meta.what}</div>}
          {reason.text && <p className="text">{reason.text}</p>}
        </div>
        {dismissed ? <span className="badge sev-NONE"><XCircle size={13} /> Dismissed</span> : <SeverityBadge level={reason.severity} />}
      </div>
      {reason.evidence.length > 0 && (
        <div className="ev">
          {reason.evidence.map((e) => <EvidenceChip key={e} refId={e} highlight={reason.text} />)}
          {reason.confidence !== null && <span className="chip static">confidence {Math.round(reason.confidence * 100)}%</span>}
        </div>
      )}
      {note && (
        <div className="skeptic">
          <span className="who"><Scale size={14} /> Skeptic {note.ruling}</span>
          {note.reason}
          {note.evidence.length > 0 && (
            <div className="row-wrap" style={{ marginTop: 8 }}>{note.evidence.map((e) => <EvidenceChip key={e} refId={e} />)}</div>
          )}
        </div>
      )}
      {!note && !dismissed && reason.severity !== "LOW" && reason.flag === "F11" && (
        <div className="skeptic"><span className="who"><ShieldAlert size={14} /> Watchlist</span>Being named in an order is not a finding against this company.</div>
      )}
    </article>
  );
}

// -- financials ---------------------------------------------------------------

/** FY23 stays; a stub period "S2023-12-31" reads "to 31 Dec 2023". */
const period = (label: string) => (label.startsWith("S") ? `to ${date(label.slice(1))}` : label);

function Financials({ cf }: { cf: CaseFile }) {
  const groups = useMemo(() => {
    const out: Record<string, { years: string[]; rows: Map<string, Map<string, Fact>> }> = {};
    for (const f of cf.facts) {
      const kind = f.field.split(".")[0];
      if (!f.fy || !STATEMENTS[kind]) continue;
      const g = (out[kind] ??= { years: [], rows: new Map() });
      if (!g.years.includes(f.fy)) g.years.push(f.fy);
      if (!g.rows.has(f.field)) g.rows.set(f.field, new Map());
      g.rows.get(f.field)!.set(f.fy, f);
    }
    for (const g of Object.values(out)) g.years.sort((a, b) => (a.startsWith("S") === b.startsWith("S") ? b.localeCompare(a) : a.startsWith("S") ? -1 : 1));
    return out;
  }, [cf]);
  const undated = cf.facts.filter((f) => !f.fy);
  if (!cf.facts.length) {
    return <div className="card"><Empty title="No financials read">This run did not read the RHP's financial statements (LIGHT depth, or no RHP text in the archive).</Empty></div>;
  }
  return (
    <div className="stack">
      <div className="callout">
        <CheckCircle2 size={18} className="ok-ic" />
        <div>Amounts in ₹ crore, from the restated statements. A period is <b>verified</b> when its accounting identities hold
          (assets = equity + liabilities, opening cash + flows = closing cash, the P&L adds up); unverified numbers are shown in grey and never used for scoring.</div>
      </div>
      {Object.entries(groups).map(([kind, g]) => {
        const checks = cf.verification.filter((v) => v.statement === kind);
        const page = [...g.rows.values()][0]?.values().next().value?.page;
        return (
          <Card key={kind} title={STATEMENTS[kind]} pad={false}
            action={<div className="row-wrap">{page && <EvidenceChip refId={`rhp:p${page}`} />}</div>}>
            <div className="table-wrap">
              <table className="fin-table">
                <thead>
                  <tr>
                    <th>Line item</th>
                    {g.years.map((y) => {
                      const c = checks.find((v) => v.fy === y);
                      return (
                        <th key={y} className="num" title={c ? `${c.passed ? "verified" : "failed"}: ${c.detail}` : "not checked"}>
                          <span className="row" style={{ justifyContent: "flex-end", gap: 4 }}>
                            {c?.passed ? <CheckCircle2 size={12} className="ok-ic" /> : c ? <XCircle size={12} className="warn-ic" /> : null}{period(y)}
                          </span>
                        </th>
                      );
                    })}
                  </tr>
                </thead>
                <tbody>
                  {[...g.rows.entries()].map(([field, byYear]) => (
                    <tr key={field}>
                      <td>{FIELD_NAMES[field] ?? field}</td>
                      {g.years.map((y) => {
                        const f = byYear.get(y);
                        return (
                          <td key={y} className={cx("num mono", f && !f.verified && "v-no")} title={f ? `${f.method}, page ${f.page}${f.verified ? "" : ", unverified"}` : ""}>
                            {f ? num(f.value) : "–"}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {checks.length > 0 && (
              <div className="card-body small muted row-wrap" style={{ borderTop: "1px solid var(--line)" }}>
                {checks.map((c, i) => (
                  <span key={i} className="pill" title={c.detail}>
                    {c.passed ? <CheckCircle2 size={12} className="ok-ic" /> : <XCircle size={12} className="warn-ic" />} {period(c.fy)}: {c.detail || "the identities did not hold"}
                  </span>
                ))}
              </div>
            )}
          </Card>
        );
      })}
      {undated.length > 0 && (
        <Card title="Other facts">
          {undated.map((f) => (
            <div className="between small" key={f.field} style={{ padding: "4px 0" }}>
              <span>{f.field === "biz.top10_customers_pct" ? "Top 10 customers' share of revenue" : f.field}</span>
              <span className="row">
                <span className={cx("mono", !f.verified && "faint")}>{num(f.value)}{f.unit === "%" ? "%" : ` ${f.unit}`}</span>
                {!f.verified && <span className="pill" title="Printed only once in the RHP">unverified</span>}
                {f.page && <EvidenceChip refId={`rhp:p${f.page}`} highlight={String(f.value)} />}
              </span>
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}

// -- trace --------------------------------------------------------------------

function TraceTab({ trace }: { trace: TraceEvent[] }) {
  const stats = useRunStats(trace);
  const [filter, setFilter] = useState("all");
  const agents = [...new Set(trace.map((e) => e.agent_name))];
  const shown = trace.filter((e) => filter === "all" || e.agent_name === filter);
  if (!trace.length) return <Empty title="No trace for this run" />;
  return (
    <div className="stack">
      <Card title="Where the time went" hint="Each agent's step, on the run's clock">
        <AgentTimeline trace={trace} />
      </Card>
      {stats.calls.length > 0 && (
        <Card title="Nemotron calls" hint="Through Meterless Nightshift; cached or replayed replies are marked" pad={false}>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Purpose</th><th>Model</th><th className="num">Tokens in</th><th className="num">Tokens out</th><th className="num">Latency</th><th>Source</th></tr></thead>
              <tbody>
                {stats.calls.map((e, i) => (
                  <tr key={i}>
                    <td className="mono small">{String(e.detail?.purpose)}</td>
                    <td className="mono tiny">{String(e.detail?.served_model ?? "").replace("nvidia/", "")}</td>
                    <td className="num mono small">{Number(e.detail?.tokens_in).toLocaleString()}</td>
                    <td className="num mono small">{Number(e.detail?.tokens_out).toLocaleString()}</td>
                    <td className="num mono small">{(Number(e.detail?.latency_ms) / 1000).toFixed(1)} s</td>
                    <td className="small">{e.detail?.replayed ? "replay" : e.detail?.cached ? "cache" : "live"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
      <Card title="Zetaris queries" hint="Named templates through the Data Port; the Leakage Guard drops rows dated after as_of" pad={false}>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Template</th><th>Parameters</th><th className="num">Rows</th><th>Served by</th><th>Ref</th></tr></thead>
            <tbody>
              {stats.queries.map((e, i) => {
                const [tpl, rows] = e.output_summary.split(" -> ");
                return (
                  <tr key={i}>
                    <td className="mono small">{tpl}</td>
                    <td className="mono tiny muted" style={{ maxWidth: 260, overflowWrap: "anywhere" }}>{e.input_summary}</td>
                    <td className="num small">{rows?.replace(" rows", "")}</td>
                    <td className="small">{String(e.detail?.source ?? "")}</td>
                    <td><EvidenceChip refId={String(e.detail?.ref)} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="Every event" pad={false} action={
        <select className="btn btn-sm" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter by agent">
          <option value="all">All agents ({trace.length})</option>
          {agents.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
      }>
        <div className="feed" style={{ maxHeight: 520 }}>
          {shown.map((e, i) => (
            <div key={i} className={cx("feed-item", (e.status === "error" || e.status === "errored") && "err")}>
              <span className="ts">{e.timestamp.slice(11, 19)}</span>
              <span className="who" title={e.action}>{e.agent_name}</span>
              <span className="msg"><span className="mono tiny faint">{e.action}</span> {e.output_summary}</span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

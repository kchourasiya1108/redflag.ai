import { useMemo, useState, type ReactNode } from "react";
import {
  ArrowRight, CheckCircle2, ChevronLeft, CircleSlash, Download, FileJson, HelpCircle, Info, RotateCcw,
  Scale, ShieldAlert, Sparkles, XCircle,
} from "lucide-react";
import { api } from "../api";
import { AgentTimeline, CashProfitChart, Contributions, OddsGrid, PartyGraph } from "../components/Charts";
import { EvidenceChip, EvidenceProvider } from "../components/Evidence";
import { Card, Empty, ErrorBox, SeverityBadge, Stat } from "../components/ui";
import type { CaseFile, Explain, Fact, Reason, RunOutput, RunResult, SkepticNote, TraceEvent } from "../types";
import {
  CONCERN_TEXT, DEPTH_TEXT, FIELD_NAMES, FLAGS, Link, RATING_TEXT, STATEMENTS, ago, cx, date, navigate, notCheckedText, num,
  odds, pct, plainText, rupees, seconds, sentence,
} from "../lib";
import { titleCase } from "./Home";
import { TechLog, useRunStats } from "./Run";

type Tab = "concerns" | "financials" | "score" | "parties";

export function Report({ result, cf, explain, trace }: {
  result: RunResult; cf: CaseFile | null; explain: Explain | null; trace: TraceEvent[];
}) {
  const o = result.output;
  const [tab, setTab] = useState<Tab>("concerns");
  const sample = o.data_source.startsWith("sample");
  const bands = explain?.bands ?? { low_below: 0.1154, high_at: 0.2308 };
  const company = titleCase(o.ipo.company);
  const dismissed = o.skeptic_notes.filter((n) => n.ruling === "dismissed");
  const cleared = o.cleared_checks.filter((c) => !dismissed.some((d) => d.flag === c.flag));
  const factCount = cf?.facts.filter((f) => f.verified).length ?? 0;

  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "concerns", label: "Concerns", count: o.reasons.length },
    { id: "financials", label: "Financials" },
    { id: "score", label: "Why this rating" },
    { id: "parties", label: "Who's involved", count: cf?.entities.filter((e) => e.type !== "sebi_order").length },
  ];

  return (
    <EvidenceProvider ipoId={o.ipo.ipo_id} trace={trace}>
      <div className="container stack-lg">
        <div className="stack-sm">
          <Link to={`/ipo/${o.ipo.ipo_id}`} className="small row"><ChevronLeft size={14} /> {company}</Link>
          <div className="between" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
            <div className="stack-sm">
              <div className="small faint">{o.ipo.exchange} · {rupees(o.ipo.issue_price)} a share · lead manager {titleCase(o.ipo.banker_raw)}</div>
              <h1>{company}</h1>
              <div className="small muted">Rated as of <b>{date(o.as_of)}</b>, the day the bid opened, using only what was known then.</div>
            </div>
            <div className="row-wrap">
              <a className="btn btn-sm" href={`/api/runs/${result.run_id}/report.md`} download={`${o.ipo.symbol}-redflag-report.md`}><Download size={14} /> Download report</a>
              <RerunButton ipoId={o.ipo.ipo_id} sample={sample} />
            </div>
          </div>
        </div>

        {result.status === "partial" && (
          <div className="callout"><Info size={18} /><div>This check hit its time limit, so it was rated on what it had read by then. Some checks may be missing.</div></div>
        )}

        <Card>
          <div className="verdict">
            <div className="stack">
              <div className="row-wrap" style={{ gap: 14 }}>
                <SeverityBadge level={o.rating} size="xl" />
              </div>
              <div className="verdict-head">{RATING_TEXT[o.rating]?.headline}</div>
              <div className="counts">
                <span><b>{o.reasons.length}</b> concern{o.reasons.length === 1 ? "" : "s"}</span>
                <span><b>{cleared.length + dismissed.length}</b> check{cleared.length + dismissed.length === 1 ? "" : "s"} passed or explained</span>
                <span><b>{o.not_checked.length}</b> could not be checked</span>
              </div>
              <p className="small muted" style={{ maxWidth: 520 }}>
                "Went badly" means the share price fell sharply in the first six months, trading was suspended,
                or the company faced SEBI action. A typical SME IPO went badly {odds(bands.low_below)} times.{" "}
                <Link to="/track-record">How accurate are these ratings?</Link>
              </p>
            </div>
            <div className="odds">
              <div className="eyebrow">Chance of a bad outcome</div>
              <div className="n">{odds(o.p_adverse)}</div>
              <OddsGrid p={o.p_adverse} rating={o.rating} />
              <div className="tiny faint" style={{ maxWidth: 300 }}>Each dot is one IPO with a profile like this one; filled dots are the ones that went badly.</div>
            </div>
          </div>
        </Card>

        <div className="report-grid">
          <div className="stack">
            <SummaryCard output={o} dismissed={dismissed.length} cleared={cleared.length} />
            <div className="tabs" role="tablist">
              {tabs.map((t) => (
                <button key={t.id} role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}>
                  {t.label} {t.count !== undefined && <span className="count">{t.count}</span>}
                </button>
              ))}
            </div>
            {tab === "concerns" && <Concerns output={o} dismissed={dismissed} />}
            {tab === "financials" && (cf ? <Financials cf={cf} verified={factCount} /> : <Empty title="No financials for this check" />)}
            {tab === "score" && (explain ? (
              <Card title="What moved the rating" hint="The factors our rating model weighs, and which way each one pushed for this IPO">
                <Contributions explain={explain} baseRate={bands.low_below} />
              </Card>
            ) : <Empty title="No breakdown for this check" />)}
            {tab === "parties" && (cf ? (
              <Card title="Who's involved" hint="Red: named in a SEBI order dated before the bid. Being named is not proof of wrongdoing.">
                {cf.entities.length ? <PartyGraph cf={cf} company={company} /> : <Empty title="No parties recorded" />}
              </Card>
            ) : <Empty title="No parties for this check" />)}
          </div>

          <aside className="stack">
            <Questions output={o} />
            <Card title="About this rating">
              <div className="stack-sm small">
                <Row k="Judged as of" v={date(o.as_of)} />
                <Row k="How deeply we read" v={DEPTH_TEXT[o.depth] ?? o.depth} />
                <Row k="Data" v={sample ? "Saved data (replay)" : "Live data"} />
                <Row k="Checked" v={result.execution_time_seconds ? `in ${seconds(result.execution_time_seconds)}` : "–"} />
              </div>
            </Card>
            <p className="tiny faint">{o.disclaimer}</p>
          </aside>
        </div>

        <details className="disclosure">
          <summary>Technical details and audit trail</summary>
          <Technical result={result} trace={trace} />
        </details>
      </div>
    </EvidenceProvider>
  );
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return <div className="between" style={{ alignItems: "baseline" }}><span className="faint">{k}</span><span style={{ textAlign: "right" }}>{v}</span></div>;
}

function RerunButton({ ipoId, sample }: { ipoId: string; sample: boolean }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <>
      <button className="btn btn-sm" disabled={busy} onClick={async () => {
        setBusy(true);
        setErr(null);
        try {
          navigate(`/run/${(await api.start(ipoId, sample)).run_id}`);
        } catch (e) {
          setErr((e as Error).message);
        }
        setBusy(false);
      }}>
        <RotateCcw size={14} /> Check again
      </button>
      {err && <ErrorBox error={err} />}
    </>
  );
}

function SummaryCard({ output: o, dismissed, cleared }: { output: RunOutput; dismissed: number; cleared: number }) {
  const plain = useMemo(() => {
    const parts: string[] = [];
    if (o.reasons.length) {
      const list = o.reasons.map((r) => `${lowerFirst(FLAGS[r.flag]?.title ?? r.title)} (${CONCERN_TEXT[r.severity]?.toLowerCase()})`);
      parts.push(`We found ${o.reasons.length} concern${o.reasons.length === 1 ? "" : "s"} that held up after we challenged ${o.reasons.length === 1 ? "it" : "them"}: ${list.join("; ")}.`);
    } else parts.push("No concern held up after we challenged it.");
    if (dismissed) parts.push(`${dismissed} other possible concern${dismissed === 1 ? " was" : "s were"} explained by the prospectus itself.`);
    parts.push(`${cleared} check${cleared === 1 ? "" : "s"} passed, and ${o.not_checked.length} could not be done; see below for what was missing.`);
    return parts.join(" ");
  }, [o, dismissed, cleared]);
  return (
    <Card>
      <div className="stack-sm">
        <div className="eyebrow">In short</div>
        <p className="summary">{o.summary ? plainText(o.summary) : plain}</p>
        <div className="row tiny faint">
          {o.summary ? <><Sparkles size={13} /> Written by AI; every number in it was checked against the prospectus data.</> : "Written from the findings below."}
        </div>
      </div>
    </Card>
  );
}

/** "Linked to a SEBI order" -> "linked to a SEBI order": only the first letter. */
const lowerFirst = (t: string) => t.charAt(0).toLowerCase() + t.slice(1);

function Questions({ output: o }: { output: RunOutput }) {
  const qs = useMemo(() => {
    const out = [...o.questions];
    for (const r of o.reasons) {
      const ask = FLAGS[r.flag]?.ask;
      if (ask && !out.includes(ask)) out.push(ask);
    }
    return out.slice(0, 6);
  }, [o]);
  if (!qs.length) return null;
  return (
    <Card title={<span className="row"><HelpCircle size={16} /> Questions to ask before you bid</span>} hint="Put these to the company, the lead manager or your adviser">
      <ol className="qlist">{qs.map((q, i) => <li key={i}><span>{q}</span></li>)}</ol>
    </Card>
  );
}

// -- concerns -----------------------------------------------------------------

function Concerns({ output: o, dismissed }: { output: RunOutput; dismissed: SkepticNote[] }) {
  const note = (flag: string) => o.skeptic_notes.find((n) => n.flag === flag);
  const dismissedIds = new Set(dismissed.map((d) => d.flag));
  const cleared = o.cleared_checks.filter((c) => !dismissedIds.has(c.flag));
  return (
    <div className="stack-lg">
      <section className="stack-sm">
        <div className="section-title" style={{ marginBottom: 4 }}>
          <h2>Concerns</h2>
          <span className="hint">Tap a source to see the page or record behind it</span>
        </div>
        {o.reasons.length === 0 && (
          <div className="card"><Empty icon={<CheckCircle2 color="var(--low)" />} title="No concern held up">Every check either passed or was explained by the prospectus itself.</Empty></div>
        )}
        {o.reasons.map((r) => <ConcernCard key={r.flag} reason={r} note={note(r.flag)} />)}
      </section>

      {dismissed.length > 0 && (
        <section className="stack-sm">
          <div className="section-title" style={{ marginBottom: 4 }}>
            <h2>Raised, then explained</h2>
            <span className="hint">Looked like a concern, but the prospectus gives an ordinary reason</span>
          </div>
          {dismissed.map((d) => {
            const c = o.cleared_checks.find((x) => x.flag === d.flag);
            return (
              <ConcernCard key={d.flag} explained note={d}
                reason={{ flag: d.flag, title: c?.title ?? d.flag, severity: "LOW", text: c?.text ?? "", evidence: c?.evidence ?? [], confidence: null }} />
            );
          })}
        </section>
      )}

      <div className="grid-2">
        <Card title={<span className="row"><CheckCircle2 size={16} className="ok-ic" /> What looks fine</span>}>
          {cleared.length === 0 && <p className="small muted">None.</p>}
          {cleared.map((c) => (
            <div className="check-row" key={c.flag}>
              <span className="ic ok-ic"><CheckCircle2 size={16} /></span>
              <div className="stack-sm" style={{ gap: 4 }}>
                <div className="small"><b>{FLAGS[c.flag]?.title ?? c.title}</b></div>
                <div className="small muted">{sentence(c.text)}</div>
                <div className="sources">{c.evidence.slice(0, 3).map((e) => <EvidenceChip key={e} refId={e} highlight={c.text} />)}</div>
              </div>
            </div>
          ))}
        </Card>
        <Card title={<span className="row"><CircleSlash size={16} className="off-ic" /> What we couldn't check</span>} hint="We say so plainly rather than skip it">
          {o.not_checked.map((n, i) => {
            const t = notCheckedText(n);
            return (
              <div className="check-row" key={i}>
                <span className="ic off-ic"><CircleSlash size={15} /></span>
                <div className="small"><b>{t.title}</b>{t.reason && <div className="muted">{t.reason}</div>}</div>
              </div>
            );
          })}
        </Card>
      </div>
    </div>
  );
}

const RULING: Record<string, { label: string; text: string }> = {
  upheld: { label: "The concern stands", text: "We looked for an ordinary explanation in the prospectus and did not find one." },
  downgraded: { label: "Softened", text: "The prospectus partly explains this, so we lowered its weight." },
  dismissed: { label: "Explained", text: "The prospectus gives an ordinary reason, so this does not count against the IPO." },
};

function ConcernCard({ reason, note, explained }: { reason: Reason; note?: SkepticNote; explained?: boolean }) {
  const meta = FLAGS[reason.flag];
  const ruling = note && (RULING[note.ruling] ?? { label: sentence(note.ruling), text: "" });
  return (
    <article className={cx("concern", explained && "explained")}>
      <div className="top">
        <div style={{ minWidth: 0 }}>
          <div className="title">{meta?.title ?? reason.title}</div>
        </div>
        {explained ? <span className="badge sev-NONE"><XCircle size={13} /> Explained</span> : <SeverityBadge level={reason.severity} kind="concern" />}
      </div>
      <div className="body">
        {reason.text && <p className="found">{sentence(reason.text)}</p>}
        {meta?.why && !explained && <p className="why"><b>Why it matters:</b> {meta.why}</p>}
        {reason.evidence.length > 0 && (
          <div className="sources">
            <span className="lbl">Sources</span>
            {reason.evidence.map((e) => <EvidenceChip key={e} refId={e} highlight={reason.text} />)}
          </div>
        )}
      </div>
      {ruling && note && (
        <div className="counter">
          <div><span className="who"><Scale size={14} /> Our counter-check: {ruling.label}.</span> {ruling.text}</div>
          <details className="disclosure">
            <summary>Read the reasoning</summary>
            <p style={{ marginTop: 8 }}>{note.reason}</p>
            {note.evidence.length > 0 && <div className="sources" style={{ marginTop: 8 }}>{note.evidence.map((e) => <EvidenceChip key={e} refId={e} />)}</div>}
          </details>
        </div>
      )}
      {!note && !explained && reason.flag === "F11" && (
        <div className="counter"><div><span className="who"><ShieldAlert size={14} /> Note:</span> being named in an order is not a finding against this company.</div></div>
      )}
    </article>
  );
}

// -- financials ---------------------------------------------------------------

/** FY23 stays; a stub period "S2023-12-31" reads "to 31 Dec 2023". */
const period = (label: string) => (label.startsWith("S") ? `to ${date(label.slice(1))}` : label);

function pick(cf: CaseFile, field: string, fy: string): Fact | undefined {
  return cf.facts.find((f) => f.field === field && f.fy === fy);
}

function AtAGlance({ cf }: { cf: CaseFile }) {
  const years = [...new Set(cf.facts.filter((f) => f.fy?.startsWith("FY")).map((f) => f.fy))].sort();
  const series = years.map((y) => ({
    label: y,
    rev: pick(cf, "pl.revenue", y)?.value ?? null,
    pat: pick(cf, "pl.pat", y)?.value ?? null,
    cfo: pick(cf, "cf.cfo", y)?.value ?? null,
  }));
  const last = series[series.length - 1], prev = series[series.length - 2];
  if (!last) return null;
  const growth = last.rev !== null && prev?.rev ? last.rev / prev.rev - 1 : null;
  const margin = last.rev && last.pat !== null ? last.pat / last.rev : null;
  const cashRatio = last.pat && last.cfo !== null ? last.cfo / last.pat : null;
  const hasCash = series.some((s) => s.cfo !== null);
  return (
    <Card title="At a glance" hint={`${last.label}, the latest full year, in ₹ crore`}>
      <div className="stack">
        <div className="glance">
          <Stat label="Sales" value={last.rev === null ? "–" : `₹${num(last.rev)} cr`}
            sub={growth === null ? undefined : `${growth >= 0 ? "up" : "down"} ${pct(Math.abs(growth), 0)} on ${prev.label}`} />
          <Stat label="Profit after tax" value={last.pat === null ? "–" : `₹${num(last.pat)} cr`}
            sub={margin === null ? undefined : `${pct(margin, 1)} of sales`} />
          <Stat label="Cash from operations" value={last.cfo === null ? "–" : `₹${num(last.cfo)} cr`}
            sub={cashRatio === null ? "not read for this year" : cashRatio < 0 ? "cash went out while profit was reported" : `₹${num(cashRatio, 2)} of cash per ₹1 of profit`} />
        </div>
        {hasCash && series.length > 1 && (
          <div className="stack-sm">
            <div className="small muted">Healthy businesses usually bring in roughly as much cash as the profit they report. A big, lasting gap is worth asking about.</div>
            <CashProfitChart years={series.map((s) => ({ label: s.label, pat: s.pat, cfo: s.cfo }))} />
          </div>
        )}
      </div>
    </Card>
  );
}

function Financials({ cf, verified }: { cf: CaseFile; verified: number }) {
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
    return (
      <div className="card"><Empty title="We didn't read the financial statements for this IPO">
        Either the early checks were enough to rate it, or the prospectus text is not on file.
      </Empty></div>
    );
  }
  return (
    <div className="stack">
      <AtAGlance cf={cf} />
      {undated.length > 0 && (
        <Card title="Other facts from the prospectus">
          {undated.map((f) => (
            <div className="between small" key={f.field} style={{ padding: "4px 0" }}>
              <span>{f.field === "biz.top10_customers_pct" ? "Top 10 customers' share of sales" : f.field}</span>
              <span className="row">
                <span className={cx("mono", !f.verified && "faint")}>{num(f.value)}{f.unit === "%" ? "%" : ` ${f.unit}`}</span>
                {!f.verified && <span className="pill" title="Printed only once in the prospectus">not cross-checked</span>}
                {f.page && <EvidenceChip refId={`rhp:p${f.page}`} highlight={String(f.value)} />}
              </span>
            </div>
          ))}
        </Card>
      )}
      <details className="disclosure">
        <summary>Full restated statements ({verified} cross-checked figures)</summary>
        <div className="stack" style={{ marginTop: 12 }}>
          <div className="callout">
            <CheckCircle2 size={18} className="ok-ic" />
            <div>Amounts in ₹ crore, from the restated statements in the prospectus. A year gets a tick when its figures add up
              (assets equal equity plus liabilities, cash flows reconcile, the profit and loss adds up). Figures that don't are shown in grey and are never used in the rating.</div>
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
                            <th key={y} className="num" title={c ? (c.passed ? "Figures add up" : "Figures do not add up") : "Not cross-checked"}>
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
                              <td key={y} className={cx("num mono", f && !f.verified && "v-no")} title={f ? `Prospectus page ${f.page}${f.verified ? "" : ", not cross-checked"}` : ""}>
                                {f ? num(f.value) : "–"}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            );
          })}
        </div>
      </details>
    </div>
  );
}

// -- technical ----------------------------------------------------------------

function Technical({ result, trace }: { result: RunResult; trace: TraceEvent[] }) {
  const o = result.output;
  const stats = useRunStats(trace);
  return (
    <div className="stack" style={{ marginTop: 14 }}>
      <div className="grid-2">
        <Card title="This check">
          <div className="stack-sm small">
            <Row k="Check ID" v={<span className="mono">{result.run_id}</span>} />
            <Row k="Data source" v={o.data_source} />
            <Row k="Rating model" v={<span className="mono">{o.model_version}</span>} />
            <Row k="Probability" v={<span className="mono">{pct(o.p_adverse, 2)}</span>} />
            <Row k="Depth" v={o.depth} />
            <Row k="Zetaris queries" v={stats.queries.length} />
            <Row k="Nemotron calls" v={`${stats.calls.length} · ${(stats.tokens / 1000).toFixed(1)}k tokens`} />
            {stats.models.map((m) => <Row key={m} k="" v={<span className="mono tiny">{m.replace("nvidia/", "")}</span>} />)}
            <Row k="Finished" v={ago(Date.parse(trace[trace.length - 1]?.timestamp ?? "") / 1000) || "–"} />
          </div>
          <div className="row-wrap" style={{ marginTop: 12 }}>
            <a className="btn btn-sm" href={`/api/runs/${result.run_id}/case`} download={`${result.run_id}-case_file.json`}><FileJson size={14} /> Case file (JSON)</a>
          </div>
        </Card>
        <Card title={`Agents (${result.agents.length})`}>
          <div className="stack-sm">
            {result.agents.map((a) => (
              <div key={a.name} className="small">
                <div className="row" style={{ gap: 6 }}><b>{a.name}</b>{a.uses_llm && <span className="pill" style={{ height: 20, fontSize: 11 }}><Sparkles size={11} /> Nemotron</span>}</div>
                <div className="faint tiny">{a.role}</div>
              </div>
            ))}
          </div>
        </Card>
      </div>
      {trace.length > 1 && (
        <Card title="Where the time went" hint="Each agent's step, on the check's clock">
          <AgentTimeline trace={trace} />
        </Card>
      )}
      {trace.length > 0 && <TechLog events={trace} />}
      <Link to="/how#technology" className="small row">How the system works <ArrowRight size={14} /></Link>
    </div>
  );
}

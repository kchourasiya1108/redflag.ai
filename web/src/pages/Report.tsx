import { useMemo, useState, type ReactNode } from "react";
import {
  CheckCircle2, ChevronLeft, CircleSlash, Download, HelpCircle, Info, RotateCcw,
  Scale, ShieldAlert, Sparkles, XCircle,
} from "lucide-react";
import { api } from "../api";
import { CashProfitChart, Contributions, OddsGrid } from "../components/Charts";
import { EvidenceChip, EvidenceProvider } from "../components/Evidence";
import { Card, Empty, ErrorBox, SeverityBadge, Stat } from "../components/ui";
import type { CaseFile, Explain, Fact, Reason, RunOutput, RunResult, SkepticNote, TraceEvent } from "../types";
import {
  CONCERN_TEXT, FIELD_NAMES, FLAGS, Link, RATING_TEXT, STATEMENTS, cx, date, navigate, notCheckedText, num,
  odds, pct, plainText, rupees, sentence, useAsync,
} from "../lib";
import { titleCase } from "./Home";

type Tab = "concerns" | "financials" | "score";

export function Report({ result, cf, explain, trace }: {
  result: RunResult; cf: CaseFile | null; explain: Explain | null; trace: TraceEvent[];
}) {
  const o = result.output;
  const [tab, setTab] = useState<Tab>("concerns");
  const bands = explain?.bands ?? { low_below: 0.1154, high_at: 0.2308 };
  const company = titleCase(o.ipo.company);
  const dismissed = o.skeptic_notes.filter((n) => n.ruling === "dismissed");
  const cleared = o.cleared_checks.filter((c) => !dismissed.some((d) => d.flag === c.flag));
  const offline = !o.data_source.startsWith("sample") && o.data_source.includes("offline");
  const upcoming = !!o.ipo.open_date && o.ipo.open_date !== "None" && o.as_of < o.ipo.open_date.slice(0, 10);

  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "concerns", label: "Concerns", count: o.reasons.length },
    { id: "financials", label: "Financials" },
    { id: "score", label: "Why this rating" },
  ];

  return (
    <EvidenceProvider ipoId={o.ipo.ipo_id} trace={trace}>
      <div className="container stack-lg">
        <div className="between" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="stack-sm">
            <Link to="/reports" className="small row"><ChevronLeft size={14} /> My reports</Link>
            <h1>{company}</h1>
            <div className="small muted">
              {upcoming ? <>Checked {date(o.as_of)}, before the bid opens on {date(o.ipo.open_date)}</>
                : <>Judged on what was known on {date(o.as_of)}, the day the bid opened</>}
            </div>
          </div>
          <div className="row-wrap">
            <a className="btn btn-sm" href={`/api/runs/${result.run_id}/report.md`} download={`${o.ipo.symbol || company.replace(/\W+/g, "-")}-redflag-report.md`}><Download size={14} /> Download</a>
            <RerunButton ipoId={o.ipo.ipo_id} />
          </div>
        </div>

        {(result.status === "partial" || offline) && (
          <div className="callout"><Info size={18} /><div>
            {result.status === "partial" ? "This check hit its time limit, so some checks may be missing. " : ""}
            {offline ? "Our live data was unavailable, so the prospectus sections that need AI reading were skipped. Check again later for a fuller report." : ""}
          </div></div>
        )}

        <Card>
          <div className="verdict">
            <div className="stack">
              <span style={{ alignSelf: "flex-start" }}><SeverityBadge level={o.rating} size="xl" /></span>
              <div className="verdict-head">{RATING_TEXT[o.rating]?.headline}</div>
              <p className="small muted" style={{ maxWidth: 520 }}>
                A typical SME IPO has a {odds(bands.low_below)} chance of going badly: a sharp fall in its first six months,
                a trading suspension, or SEBI action. <Link to="/how#track-record">How accurate is this?</Link>
              </p>
            </div>
            <div className="odds">
              <div className="eyebrow">Chance of a bad outcome</div>
              <div className="n">{odds(o.p_adverse)}</div>
              <OddsGrid p={o.p_adverse} rating={o.rating} />
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
            {tab === "financials" && (cf ? <Financials cf={cf} /> : <Empty title="No financials for this check" />)}
            {tab === "score" && (explain ? (
              <Card title="What moved the rating">
                <Contributions explain={explain} baseRate={bands.low_below} />
              </Card>
            ) : <Empty title="No breakdown for this check" />)}
          </div>

          <aside className="stack">
            <Questions output={o} />
            <AboutIpo ipoId={o.ipo.ipo_id} />
          </aside>
        </div>
      </div>
    </EvidenceProvider>
  );
}

/** The facts an investor bids on: price, lot, dates and who runs the issue. */
function AboutIpo({ ipoId }: { ipoId: string }) {
  const { data } = useAsync(() => api.ipo(ipoId), [ipoId]);
  if (!data) return null;
  const i = data.ipo;
  const price = Number(i.issue_price), lot = Number(i.lot_size);
  const rows: [string, string | null][] = [
    ["Exchange", i.exchange as string],
    ["Bid opens", i.open_date ? date(i.open_date as string) : "Not announced"],
    ["Price per share", price ? rupees(price) : "Not announced"],
    ["Minimum investment", price && lot ? rupees(price * lot) : null],
    ["Lot size", lot ? `${lot.toLocaleString("en-IN")} shares` : null],
    ["Lead manager", i.banker_raw ? titleCase(String(i.banker_raw)) : null],
    ["Registrar", i.registrar_raw ? titleCase(String(i.registrar_raw)) : null],
    ["Market maker", i.market_maker_raw ? titleCase(String(i.market_maker_raw)) : null],
    ["Subscribed", i.final_subscription_x ? `${Number(i.final_subscription_x).toFixed(1)}× (context only, not rated)` : null],
  ];
  return (
    <Card title="About this IPO">
      <div className="stack-sm small">
        {rows.filter(([, v]) => v).map(([k, v]) => <Row key={k} k={k} v={v} />)}
      </div>
    </Card>
  );
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return <div className="between" style={{ alignItems: "baseline" }}><span className="faint">{k}</span><span style={{ textAlign: "right" }}>{v}</span></div>;
}

function RerunButton({ ipoId }: { ipoId: string }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <>
      <button className="btn btn-sm" disabled={busy} onClick={async () => {
        setBusy(true);
        setErr(null);
        try {
          navigate(`/run/${(await api.check(ipoId)).run_id}`);
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
    return parts.join(" ");
  }, [o, dismissed, cleared]);
  return (
    <Card>
      <div className="stack-sm">
        <div className="eyebrow">In short</div>
        <p className="summary">{o.summary ? plainText(o.summary) : plain}</p>
        <div className="row tiny faint">
          {o.summary && <><Sparkles size={13} /> Written by AI; every number in it was checked against the prospectus.</>}
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
    <Card title={<span className="row"><HelpCircle size={16} /> Questions to ask before you bid</span>}>
      <ol className="qlist">{qs.map((q, i) => <li key={i}><span>{q}</span></li>)}</ol>
    </Card>
  );
}

// -- concerns -----------------------------------------------------------------

function Concerns({ output: o, dismissed }: { output: RunOutput; dismissed: SkepticNote[] }) {
  const note = (flag: string) => o.skeptic_notes.find((n) => n.flag === flag);
  const dismissedIds = new Set(dismissed.map((d) => d.flag));
  const cleared = o.cleared_checks.filter((c) => !dismissedIds.has(c.flag));
  const fine = [
    ...cleared.map((c) => ({ flag: c.flag, title: FLAGS[c.flag]?.title ?? c.title, text: sentence(c.text), evidence: c.evidence })),
    ...dismissed.map((d) => {
      const c = o.cleared_checks.find((x) => x.flag === d.flag);
      return { flag: d.flag, title: FLAGS[d.flag]?.title ?? c?.title ?? d.flag, evidence: c?.evidence ?? [],
        text: `Looked like a concern, but the prospectus explains it. ${sentence(d.reason)}` };
    }),
  ];
  return (
    <div className="stack">
      {o.reasons.length === 0 && (
        <div className="card"><Empty icon={<CheckCircle2 color="var(--low)" />} title="No concerns">Every check we could run either passed or was explained by the prospectus.</Empty></div>
      )}
      {o.reasons.map((r) => <ConcernCard key={r.flag} reason={r} note={note(r.flag)} />)}

      {fine.length > 0 && (
        <details className="fold">
          <summary><CheckCircle2 size={16} className="ok-ic" /> {fine.length} check{fine.length === 1 ? "" : "s"} looked fine</summary>
          <div className="fold-body">
            {fine.map((c) => (
              <div className="check-row" key={c.flag}>
                <span className="ic ok-ic"><CheckCircle2 size={16} /></span>
                <div className="stack-sm" style={{ gap: 4 }}>
                  <div className="small"><b>{c.title}</b></div>
                  <div className="small muted">{c.text}</div>
                  <Sources refs={c.evidence} highlight={c.text} />
                </div>
              </div>
            ))}
          </div>
        </details>
      )}
      {o.not_checked.length > 0 && (
        <details className="fold">
          <summary><CircleSlash size={16} className="off-ic" /> {o.not_checked.length} thing{o.not_checked.length === 1 ? "" : "s"} we couldn't check</summary>
          <div className="fold-body">
            {o.not_checked.map((n, i) => {
              const t = notCheckedText(n);
              return (
                <div className="check-row" key={i}>
                  <span className="ic off-ic"><CircleSlash size={15} /></span>
                  <div className="small"><b>{t.title}</b>{t.reason && <div className="muted">{t.reason}</div>}</div>
                </div>
              );
            })}
          </div>
        </details>
      )}
    </div>
  );
}

/** Prospectus pages and SEBI orders only: what an investor can open and read. */
function Sources({ refs, highlight }: { refs: string[]; highlight?: string }) {
  const shown = refs.filter((r) => r.startsWith("rhp:") || r.startsWith("sebi:")).slice(0, 4);
  if (!shown.length) return null;
  return (
    <div className="sources">
      <span className="lbl">Source</span>
      {shown.map((e) => <EvidenceChip key={e} refId={e} highlight={highlight} />)}
    </div>
  );
}

const RULING: Record<string, { label: string; text: string }> = {
  upheld: { label: "Still a concern", text: "We looked for an ordinary explanation in the prospectus and did not find one." },
  downgraded: { label: "Softened", text: "The prospectus partly explains this, so we lowered its weight." },
  dismissed: { label: "Explained", text: "The prospectus gives an ordinary reason, so this does not count against the IPO." },
  corroboration: { label: "Held at moderate", text: "Nothing else in the check points the same way, so we do not rate it serious on its own." },
};

function ConcernCard({ reason, note, explained }: { reason: Reason; note?: SkepticNote; explained?: boolean }) {
  const meta = FLAGS[reason.flag];
  const ruling = note && (RULING[note.ruling] ?? { label: sentence(note.ruling).replace(/\.$/, ""), text: "" });
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
        <Sources refs={reason.evidence} highlight={reason.text} />
      </div>
      {ruling && note && (
        <div className="counter">
          <div><span className="who"><Scale size={14} /> {ruling.label}.</span> {ruling.text}</div>

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

function Financials({ cf }: { cf: CaseFile }) {
  const verified = cf.facts.filter((f) => f.verified).length;
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


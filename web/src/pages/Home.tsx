import { ArrowRight, Banknote, Building2, Gavel, Landmark, PieChart, ReceiptIndianRupee, ShieldCheck } from "lucide-react";
import { api } from "../api";
import { IpoSearch } from "../components/Search";
import { RecentUploads, UploadBox } from "./Upload";
import { SeverityBadge, Skeleton } from "../components/ui";
import type { Metrics, RunSummary } from "../types";
import { Link, RATING_TEXT, ago, date, odds, pct, rupees, useAsync } from "../lib";

const CHECKS = [
  { icon: <Gavel size={18} />, t: "SEBI orders", d: "Promoters, directors, the lead manager, auditor and market maker, screened against SEBI orders issued before the bid." },
  { icon: <Landmark size={18} />, t: "The lead manager's record", d: "How the banker's earlier SME IPOs did six months after listing." },
  { icon: <ReceiptIndianRupee size={18} />, t: "Profits vs cash", d: "Whether reported profit turns into real cash, and whether it jumped just before the IPO." },
  { icon: <Banknote size={18} />, t: "Cheap insider shares", d: "Shares given to insiders below your price in the 18 months before the issue." },
  { icon: <Building2 size={18} />, t: "Who the business depends on", d: "Customer concentration and trade with the promoters' own companies." },
  { icon: <PieChart size={18} />, t: "Price and use of money", d: "The issue's valuation against listed peers, and how much of the money has no stated use." },
];

/** Of the IPOs rated HIGH, how many went badly; and how many went badly overall. */
export function hitRates(m: Metrics) {
  const caught = m.recall_at_high * m.adverse;
  const flagged = caught + m.false_alarm_rate * (m.n - m.adverse);
  return { highHit: flagged ? caught / flagged : 0, base: m.adverse / m.n, flagged: Math.round(flagged) };
}

/** Latest finished report per IPO, newest first. */
export function latestPerIpo(runs: RunSummary[] | undefined): RunSummary[] {
  const seen = new Set<string>();
  return (runs ?? []).filter((r) => r.rating && !seen.has(r.ipo_id) && seen.add(r.ipo_id));
}

export function Home() {
  const demos = useAsync(() => api.ipos("", 10, true), []);
  const runs = useAsync(() => api.runs(), []);
  const bt = useAsync(() => api.backtest(), []);
  const latest = (id: string): RunSummary | undefined => runs.data?.runs.find((r) => r.ipo_id === id && r.rating);
  const m = bt.data?.results?.model;
  const rates = m && hitRates(m);
  const demoIds = new Set(demos.data?.items.map((d) => d.ipo_id));
  const recent = latestPerIpo(runs.data?.runs).filter((r) => !demoIds.has(r.ipo_id)).slice(0, 6);

  return (
    <div className="container stack-xl">
      <section className="hero">
        <div className="eyebrow">For investors in NSE Emerge and BSE SME IPOs</div>
        <h1>Check an upcoming SME IPO for red flags before you bid.</h1>
        <p className="lede">
          Upload the red herring prospectus (RHP). We read it, screen everyone involved against SEBI orders, and look at
          how the lead manager's past IPOs did. You get a clear risk rating, the concerns behind it, and the exact page each one comes from.
        </p>
        <UploadBox />
        <div className="stack-sm">
          <div className="small faint">Or look up a past SME IPO by company, symbol or lead manager</div>
          <IpoSearch />
        </div>
      </section>

      {rates && m && (
        <section className="trust">
          <div>
            <div className="big">{bt.data!.results!.split.test}</div>
            <div className="small muted">past SME IPOs we tested the ratings on, none of which the model had seen</div>
          </div>
          <div>
            <div className="big">{pct(rates.highHit, 0)}</div>
            <div className="small muted">of IPOs rated <b>High risk</b> went badly in their first six months, against {pct(rates.base, 0)} of all IPOs</div>
          </div>
          <div>
            <div className="big">Every claim</div>
            <div className="small muted">links to the prospectus page, SEBI order or record it came from</div>
          </div>
          <Link to="/track-record" className="small row">How we measure this <ArrowRight size={14} /></Link>
        </section>
      )}

      <section>
        <div className="section-title">
          <h2>Example reports</h2>
          <span className="hint">Three past IPOs, rated as of the day their bids opened</span>
        </div>
        <div className="grid-3">
          {demos.loading && [0, 1, 2].map((i) => <div key={i} className="card card-pad"><Skeleton h={120} /></div>)}
          {demos.data?.items.map((d) => {
            const run = latest(d.ipo_id);
            return (
              <Link key={d.ipo_id} to={run ? `/run/${run.run_id}` : `/ipo/${d.ipo_id}`} className="card demo-card">
                <div className="between">
                  {run ? <SeverityBadge level={run.rating} size="lg" /> : <span className="pill">Not rated yet</span>}
                  <span className="small faint">{d.exchange}</span>
                </div>
                <div>
                  <div className="company">{titleCase(d.company)}</div>
                  <div className="small faint">Bid opened {date(d.open_date)} · {rupees(d.issue_price)} a share</div>
                </div>
                {run && <div className="small muted">{RATING_TEXT[run.rating!]?.headline}</div>}
                <div className="between small">
                  <span className="muted">{run ? <>Chance of a bad outcome: <b>{odds(run.p_adverse)}</b></> : "Open to run a check"}</span>
                  <span className="row nowrap" style={{ gap: 4 }}>{run ? "Read report" : "Open"} <ArrowRight size={14} /></span>
                </div>
              </Link>
            );
          })}
        </div>
      </section>

      <section>
        <div className="section-title">
          <h2>What we check</h2>
          <Link to="/how" className="small">How we rate →</Link>
        </div>
        <div className="checks-grid">
          {CHECKS.map((c) => (
            <div className="check-tile" key={c.t}>
              <div className="ic">{c.icon}</div>
              <h3>{c.t}</h3>
              <p className="small muted">{c.d}</p>
            </div>
          ))}
        </div>
      </section>

      <RecentUploadsSection />

      {recent.length > 0 && (
        <section>
          <div className="section-title">
            <h2>Recently checked</h2>
            <Link to="/reports" className="small">All reports →</Link>
          </div>
          <ReportList runs={recent} />
        </section>
      )}

      <div className="callout">
        <ShieldCheck size={18} />
        <div className="small">
          RedFlag rates risk; it does not give advice. It never tells you to buy, sell or apply. Being linked to a SEBI order
          is not proof that a company did anything wrong. Read the evidence and make your own decision.
        </div>
      </div>
    </div>
  );
}

/** "VARYAA CREATIONS LIMITED" -> "Varyaa Creations Limited"; mixed case is left alone. */
export function titleCase(name: string | null | undefined): string {
  if (!name) return "";
  if (name !== name.toUpperCase()) return name;
  return name.toLowerCase().replace(/(^|[\s(&.-])([a-z])/g, (_, pre, c) => pre + c.toUpperCase());
}

export function ReportList({ runs, loading }: { runs?: RunSummary[]; loading?: boolean }) {
  if (loading && !runs) return <div className="card card-pad stack-sm">{[0, 1, 2].map((i) => <Skeleton key={i} h={20} />)}</div>;
  if (!runs?.length) return <div className="card"><div className="empty small">No reports yet. Search for an IPO to check it.</div></div>;
  return (
    <div className="card report-list">
      {runs.map((r) => (
        <Link key={r.run_id} to={`/run/${r.run_id}`} className="report-row">
          <div style={{ minWidth: 0 }}>
            <div className="name">{titleCase(r.company) || r.ipo_id}</div>
            <div className="small faint">
              {r.status === "error" ? "The check could not finish" : r.p_adverse !== undefined && r.p_adverse !== null ? `Chance of a bad outcome: ${odds(r.p_adverse)}` : ""}
              {r.sample_mode ? " · saved data" : ""}
            </div>
          </div>
          <SeverityBadge level={r.rating ?? (r.status === "error" ? "error" : r.status)} />
          <span className="small faint nowrap when">{ago(r.finished_at ?? r.started_at)}</span>
          <ArrowRight size={15} className="faint go" />
        </Link>
      ))}
    </div>
  );
}

function RecentUploadsSection() {
  return (
    <section className="uploads-section">
      <div className="section-title"><h2>Your uploaded prospectuses</h2></div>
      <RecentUploads />
    </section>
  );
}

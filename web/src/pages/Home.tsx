import { useState } from "react";
import { ArrowRight, Search } from "lucide-react";
import { api } from "../api";
import { IpoSearch } from "../components/Search";
import { UpcomingIpos, UploadBox } from "./Upload";
import { SeverityBadge, Skeleton } from "../components/ui";
import type { Metrics, RunSummary } from "../types";
import { Link, ago, odds, useAsync } from "../lib";

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
  const [lookup, setLookup] = useState(false);
  const latest = (id: string): RunSummary | undefined => runs.data?.runs.find((r) => r.ipo_id === id && r.rating);

  return (
    <div className="container stack-xl">
      <section className="hero">
        <h1>Check an SME IPO for red flags before you bid.</h1>
        <p className="lede">
          Upload the red herring prospectus (RHP). We read it, check everyone involved against SEBI orders and the lead
          manager's past IPOs, and tell you how risky it looks and why.
        </p>
        <UploadBox />
        {lookup ? <IpoSearch autoFocus /> : (
          <button className="btn btn-ghost btn-sm" style={{ alignSelf: "flex-start" }} onClick={() => setLookup(true)}>
            <Search size={14} /> Or look up a past SME IPO
          </button>
        )}
      </section>

      <UpcomingIpos />

      {(demos.data?.items.length ?? 0) > 0 && (
        <section>
          <div className="section-title"><h2>See an example report</h2></div>
          <div className="grid-3">
            {demos.data!.items.map((d) => {
              const run = latest(d.ipo_id);
              return (
                <Link key={d.ipo_id} to={run ? `/run/${run.run_id}` : `/ipo/${d.ipo_id}`} className="card demo-card">
                  {run ? <SeverityBadge level={run.rating} /> : <span className="pill">Not rated yet</span>}
                  <div className="company">{titleCase(d.company)}</div>
                  <div className="between small">
                    <span className="muted">{run ? <>Chance of a bad outcome: <b>{odds(run.p_adverse)}</b></> : d.exchange}</span>
                    <ArrowRight size={14} className="faint" />
                  </div>
                </Link>
              );
            })}
          </div>
        </section>
      )}
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


import { ArrowRight, Clock, Database, FileSearch, Scale, ShieldCheck } from "lucide-react";
import { api } from "../api";
import { IpoSearch } from "../components/Search";
import { Card, SeverityBadge, Skeleton } from "../components/ui";
import type { RunSummary } from "../types";
import { Link, ago, date, navigate, pct, rupees, seconds, useAsync } from "../lib";

export function Home() {
  const demos = useAsync(() => api.ipos("", 10, true), []);
  const runs = useAsync(() => api.runs(), []);
  const latest = (id: string): RunSummary | undefined => runs.data?.runs.find((r) => r.ipo_id === id);

  return (
    <div className="container stack-lg">
      <section className="hero">
        <div className="eyebrow">Indian SME IPOs · NSE Emerge & BSE SME</div>
        <h1>See the red flags in an SME IPO before the bid opens.</h1>
        <p className="lede">
          RedFlag reads the prospectus, checks every party against SEBI orders and the lead manager's track record,
          and gives a backtested risk rating where every claim links to the page or query it came from.
        </p>
        <IpoSearch autoFocus />
        <div className="row-wrap small faint">
          <span>A rating, not advice: RedFlag never says apply, avoid, buy or sell.</span>
        </div>
      </section>

      <section>
        <div className="section-title">
          <h2>Recorded demos</h2>
          <span className="hint">Recorded live against Zetaris and Nemotron; replayable offline in sample mode</span>
        </div>
        <div className="grid-3">
          {demos.loading && [0, 1, 2].map((i) => <div key={i} className="card card-pad"><Skeleton h={90} /></div>)}
          {demos.data?.items.map((d) => {
            const run = latest(d.ipo_id);
            return (
              <Link key={d.ipo_id} to={`/ipo/${d.ipo_id}`} className="card demo-card">
                <div className="between">
                  <span className="pill mono">{d.symbol}</span>
                  {run ? <SeverityBadge level={run.rating} /> : <span className="pill">Not run yet</span>}
                </div>
                <div>
                  <div className="company">{d.company}</div>
                  <div className="small faint">{d.exchange} · bid {date(d.open_date)} · {rupees(d.issue_price)}</div>
                </div>
                <div className="between small">
                  <span className="muted">{run ? `p = ${pct(run.p_adverse)} · ${run.depth}` : d.banker_raw}</span>
                  <span className="row" style={{ gap: 4 }}>Open <ArrowRight size={14} /></span>
                </div>
              </Link>
            );
          })}
        </div>
      </section>

      <Card pad={false}>
        <div className="how">
          {[
            { icon: <Database size={18} />, t: "Point-in-time data", d: "Every lookup runs through Zetaris with a Leakage Guard: only what was known before the bid date." },
            { icon: <FileSearch size={18} />, t: "Reads the RHP", d: "Restated financials are parsed and verified by accounting identities; Nemotron reads the narrative sections." },
            { icon: <Scale size={18} />, t: "Argues both sides", d: "A Skeptic agent looks for an ordinary explanation of each flag, citing RHP pages, before it counts." },
            { icon: <ShieldCheck size={18} />, t: "Backtested score", d: "A calibrated model trained on past SME IPOs, tested on later ones it never saw." },
          ].map((s, i) => (
            <div className="step stack-sm" key={i}>
              <div className="row" style={{ color: "var(--text-2)" }}>{s.icon}<span className="n">0{i + 1}</span></div>
              <h3>{s.t}</h3>
              <p className="small muted">{s.d}</p>
            </div>
          ))}
        </div>
      </Card>

      <section>
        <div className="section-title">
          <h2>Recent runs</h2>
          <Link to="/runs" className="small">All runs →</Link>
        </div>
        <RunList runs={runs.data?.runs.slice(0, 6)} loading={runs.loading} />
      </section>
    </div>
  );
}

export function RunList({ runs, loading }: { runs?: RunSummary[]; loading?: boolean }) {
  if (loading && !runs) return <div className="card card-pad stack-sm">{[0, 1, 2].map((i) => <Skeleton key={i} h={20} />)}</div>;
  if (!runs?.length) return <div className="card"><div className="empty small">No runs yet. Search for an IPO above to start one.</div></div>;
  return (
    <div className="card table-wrap">
      <table>
        <thead>
          <tr><th>Company</th><th>Rating</th><th className="num">p(adverse)</th><th>Depth</th><th>Data</th><th className="num">Time</th><th>When</th></tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.run_id} className="clickable" onClick={() => navigate(`/run/${r.run_id}`)}>
              <td>
                <Link to={`/run/${r.run_id}`}><b style={{ color: "var(--text)" }}>{r.company ?? r.ipo_id}</b></Link>
                <div className="tiny faint mono">{r.run_id}</div>
              </td>
              <td><SeverityBadge level={r.rating ?? (r.status === "error" ? "error" : r.status)} /></td>
              <td className="num mono">{pct(r.p_adverse)}</td>
              <td className="small">{r.depth ?? "–"}</td>
              <td><span className="pill">{r.sample_mode ? "Sample" : "Live · Zetaris"}</span></td>
              <td className="num small"><span className="row" style={{ justifyContent: "flex-end", gap: 4 }}><Clock size={12} aria-hidden />{seconds(r.execution_time_seconds)}</span></td>
              <td className="small faint nowrap">{ago(r.finished_at ?? r.started_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

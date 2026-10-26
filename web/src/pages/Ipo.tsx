import { useState } from "react";
import { ArrowRight, ChevronLeft, Eye, EyeOff, ExternalLink, Loader2, Search } from "lucide-react";
import { api } from "../api";
import { ReportList, titleCase } from "./Home";
import { Card, ErrorBox, SeverityBadge, Skeleton } from "../components/ui";
import type { Status } from "../types";
import { Link, RATING_TEXT, ago, date, navigate, odds, pct, rupees, useAsync } from "../lib";

export function IpoPage({ id, status }: { id: string; status: Status | null }) {
  const { data, error, loading } = useAsync(() => api.ipo(id), [id]);
  const liveReady = !!(status?.zetaris.ok && status?.meterless.ok);
  const [mode, setMode] = useState<"live" | "sample" | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [hindsight, setHindsight] = useState(false);

  if (loading && !data) return <div className="container stack"><Skeleton h={34} w={360} /><Skeleton h={220} /></div>;
  if (error) return <div className="container"><ErrorBox error={error} /></div>;
  if (!data) return null;
  const ipo = data.ipo;
  const chosen = mode ?? (liveReady || !data.in_sample ? "live" : "sample");
  const canStart = chosen === "sample" || liveReady || !status;
  const rhp = data.docs.find((d) => d.doc_type === "rhp" && d.status === "ok");
  const latest = data.runs.find((r) => r.rating);
  const price = Number(ipo.issue_price), lot = Number(ipo.lot_size);

  const start = async () => {
    setStarting(true);
    setStartError(null);
    try {
      const r = await api.start(id, chosen === "sample");
      navigate(`/run/${r.run_id}`);
    } catch (e) {
      const msg = (e as Error).message;
      setStartError(/busy|already/i.test(msg) ? "Another live check is running. Please wait a minute and try again." : msg);
      setStarting(false);
    }
  };

  const facts: [string, string][] = [
    ["Bid opened", date(ipo.open_date as string)],
    ["Listing date", date(ipo.listing_date as string)],
    ["Price per share", rupees(ipo.issue_price)],
    ["Minimum lot", lot ? `${lot.toLocaleString("en-IN")} shares` : "–"],
    ["Minimum investment", price && lot ? rupees(price * lot) : "–"],
    ["Issue size", ipo.issue_size_cr ? `₹${Number(ipo.issue_size_cr).toFixed(2)} crore` : "–"],
    ["Exchange", String(ipo.exchange ?? "–")],
    ["Industry", String(ipo.industry ?? "–")],
    ["Issue type", String(ipo.issue_type ?? "–")],
    ["Lead manager", String(ipo.banker_raw ?? "–")],
    ["Registrar", String(ipo.registrar_raw ?? "–")],
    ["Market maker", String(ipo.market_maker_raw ?? "–")],
  ];
  const outcome = data.outcome;

  return (
    <div className="container stack-lg">
      <div className="stack-sm">
        <Link to="/" className="small row"><ChevronLeft size={14} /> Search</Link>
        <div className="between" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
          <div className="stack-sm">
            <div className="small faint">{ipo.exchange} · symbol <span className="mono">{ipo.symbol}</span></div>
            <h1>{titleCase(String(ipo.company))}</h1>
          </div>
          {ipo.rhp_url && <a className="btn btn-sm" href={String(ipo.rhp_url)} target="_blank" rel="noreferrer">Prospectus (RHP) <ExternalLink size={13} /></a>}
        </div>
      </div>

      <div className="report-grid">
        <div className="stack">
          {latest && (
            <Link to={`/run/${latest.run_id}`} className="card latest-card">
              <SeverityBadge level={latest.rating} size="lg" />
              <div style={{ minWidth: 0 }}>
                <div className="t">{RATING_TEXT[latest.rating!]?.headline}</div>
                <div className="small muted">Chance of a bad outcome: {odds(latest.p_adverse)} · checked {ago(latest.finished_at)}</div>
              </div>
              <span className="row small nowrap">Read the report <ArrowRight size={14} /></span>
            </Link>
          )}

          <Card title="About the issue">
            <div className="kv">
              {facts.map(([k, v]) => <div key={k}><div className="k">{k}</div><div className="v">{v}</div></div>)}
            </div>
          </Card>

          <div className="hindsight stack-sm">
            <div className="between">
              <div>
                <b>What happened after listing</b>
                <div className="small muted">Hidden so you can judge the report fairly. The rating only uses what was known on the bid date.</div>
              </div>
              <button className="btn btn-sm" onClick={() => setHindsight((h) => !h)} disabled={!outcome}>
                {hindsight ? <EyeOff size={14} /> : <Eye size={14} />} {outcome ? (hindsight ? "Hide" : "Show") : "Not known yet"}
              </button>
            </div>
            {hindsight && outcome && (
              <div className="kv" style={{ marginTop: 8 }}>
                <div><div className="k">Share price after 6 months</div><div className="v mono">{signed(outcome.ret_6m as number)}</div></div>
                <div><div className="k">{String(outcome.index_name ?? "Market index")} over the same time</div><div className="v mono">{signed(outcome.index_ret_6m as number)}</div></div>
                <div><div className="k">Outcome</div><div className="v">{outcome.adverse === 1 ? "Went badly" : outcome.adverse === 0 ? "Did not go badly" : "Unknown"}</div></div>
              </div>
            )}
          </div>

          {data.runs.length > 0 && (
            <section className="stack-sm">
              <h2>Earlier reports on this IPO</h2>
              <ReportList runs={data.runs} />
            </section>
          )}
        </div>

        <Card className="check-card" title={latest ? "Check it again" : "Check this IPO"} hint={`Judged as of ${date(ipo.open_date as string)}, the day the bid opened`}>
          <div className="stack">
            <ul className="ticks small">
              <li>Screens everyone involved against SEBI orders</li>
              <li>Looks up the lead manager's past IPOs</li>
              <li>{rhp ? `Reads the ${rhp.pages}-page prospectus` : "No prospectus text on file: rates on the people involved and track records only"}</li>
              <li>Challenges each concern before it counts</li>
            </ul>
            {startError && <ErrorBox error={startError} />}
            {!canStart && (
              <div className="callout small">
                Fresh checks are offline right now.{data.in_sample ? " You can still replay the saved check below." : " Please try again later."}
              </div>
            )}
            <button className="btn btn-primary btn-lg" onClick={start} disabled={starting || !canStart}>
              {starting ? <Loader2 size={17} className="spin" /> : <Search size={17} />}
              {starting ? "Starting…" : latest ? "Run a new check" : "Check this IPO"}
            </button>
            <p className="small faint" style={{ textAlign: "center", marginTop: -6 }}>
              {chosen === "sample" ? "Instant: replays saved data" : "Usually takes about 3 minutes"}
            </p>
            <details className="disclosure">
              <summary>Check options</summary>
              <div className="stack-sm" role="radiogroup" aria-label="Data source" style={{ marginTop: 10 }}>
                <button className="mode-option" role="radio" aria-checked={chosen === "live"} onClick={() => setMode("live")}>
                  <span className="radio" />
                  <span className="t">Fresh check</span>
                  <span className="d">
                    Live data and AI reading, about 3 minutes.
                    {status && !liveReady && <><br /><span style={{ color: "var(--high-text)" }}>Live data is offline right now.</span></>}
                  </span>
                </button>
                <button className="mode-option" role="radio" aria-checked={chosen === "sample"} onClick={() => setMode("sample")} disabled={!data.in_sample}>
                  <span className="radio" />
                  <span className="t">Instant replay</span>
                  <span className="d">
                    {data.in_sample
                      ? data.recorded
                        ? "Replays a saved check from the offline archive in about a second."
                        : "Offline archive; the AI reading steps are skipped."
                      : "Not available for this IPO."}
                  </span>
                </button>
              </div>
            </details>
          </div>
        </Card>
      </div>
    </div>
  );
}

const signed = (x: number | null | undefined) => (x === null || x === undefined ? "–" : `${x > 0 ? "+" : ""}${pct(x)}`);

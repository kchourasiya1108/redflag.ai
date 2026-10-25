import { useState } from "react";
import { ChevronLeft, Eye, EyeOff, ExternalLink, Loader2, Play } from "lucide-react";
import { api } from "../api";
import { RunList } from "./Home";
import { Card, ErrorBox, Skeleton } from "../components/ui";
import type { Status } from "../types";
import { Link, date, navigate, pct, rupees, useAsync } from "../lib";

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
  const chosen = mode ?? (liveReady ? "live" : data.in_sample ? "sample" : "live");
  const rhp = data.docs.find((d) => d.doc_type === "rhp" && d.status === "ok");

  const start = async () => {
    setStarting(true);
    setStartError(null);
    try {
      const r = await api.start(id, chosen === "sample");
      navigate(`/run/${r.run_id}`);
    } catch (e) {
      setStartError((e as Error).message);
      setStarting(false);
    }
  };

  const facts: [string, string][] = [
    ["Exchange", String(ipo.exchange ?? "–")],
    ["Bid opens", date(ipo.open_date as string)],
    ["Listing", date(ipo.listing_date as string)],
    ["Issue price", rupees(ipo.issue_price)],
    ["Issue size", ipo.issue_size_cr ? `₹${Number(ipo.issue_size_cr).toFixed(2)} cr` : "–"],
    ["Issue type", String(ipo.issue_type ?? "–")],
    ["Lot size", String(ipo.lot_size ?? "–")],
    ["Industry", String(ipo.industry ?? "–")],
    ["Lead manager", String(ipo.banker_raw ?? "–")],
    ["Registrar", String(ipo.registrar_raw ?? "–")],
    ["Market maker", String(ipo.market_maker_raw ?? "–")],
    ["RHP", rhp ? `${rhp.pages} pages` : "not in the archive"],
  ];
  const outcome = data.outcome;

  return (
    <div className="container stack-lg">
      <div className="stack-sm">
        <Link to="/" className="small row" ><ChevronLeft size={14} /> Search</Link>
        <div className="between" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
          <div className="stack-sm">
            <div className="row-wrap">
              <span className="pill mono">{ipo.symbol}</span>
              <span className="pill mono">{ipo.ipo_id}</span>
              {data.recorded && <span className="pill">Recorded demo</span>}
            </div>
            <h1>{ipo.company}</h1>
          </div>
          {ipo.rhp_url && <a className="btn btn-sm" href={String(ipo.rhp_url)} target="_blank" rel="noreferrer">RHP on the exchange <ExternalLink size={13} /></a>}
        </div>
      </div>

      <div className="report-grid">
        <div className="stack">
          <Card title="The issue" hint="Cover-page facts from the archive">
            <div className="kv">
              {facts.map(([k, v]) => <div key={k}><div className="k">{k}</div><div className="v">{v}</div></div>)}
            </div>
          </Card>

          <div className="hindsight stack-sm">
            <div className="between">
              <div>
                <b>What happened after listing</b>
                <div className="small muted">Hidden by default. The run never sees this: it is judged as of the bid date.</div>
              </div>
              <button className="btn btn-sm" onClick={() => setHindsight((h) => !h)} disabled={!outcome}>
                {hindsight ? <EyeOff size={14} /> : <Eye size={14} />} {outcome ? (hindsight ? "Hide" : "Reveal") : "No outcome yet"}
              </button>
            </div>
            {hindsight && outcome && (
              <div className="kv" style={{ marginTop: 8 }}>
                <div><div className="k">6-month return</div><div className="v mono">{pct(outcome.ret_6m as number)}</div></div>
                <div><div className="k">vs {String(outcome.index_name ?? "index")}</div><div className="v mono">{pct(outcome.index_ret_6m as number)}</div></div>
                <div><div className="k">Outcome</div><div className="v">{outcome.adverse === 1 ? "Adverse" : outcome.adverse === 0 ? "Not adverse" : "Unknown"}</div></div>
                <div><div className="k">Known on</div><div className="v">{date(outcome.outcome_known_on as string)}</div></div>
              </div>
            )}
          </div>

          <section className="stack-sm">
            <h2>Earlier runs</h2>
            <RunList runs={data.runs} />
          </section>
        </div>

        <Card title="Investigate" hint={`As of ${date(ipo.open_date as string)}, the day the bid opened`}>
          <div className="stack" role="radiogroup" aria-label="Run mode">
            <button className="mode-option" role="radio" aria-checked={chosen === "live"} onClick={() => setMode("live")}>
              <span className="radio" />
              <span className="t">Live</span>
              <span className="d">
                Zetaris on the Linux PC and Nemotron through Meterless Nightshift. About 3 minutes.
                {!liveReady && status && <><br /><span style={{ color: "var(--high-text)" }}>
                  {!status.zetaris.ok ? "Zetaris is not answering." : "Nightshift is not answering."}</span></>}
              </span>
            </button>
            <button className="mode-option" role="radio" aria-checked={chosen === "sample"} onClick={() => setMode("sample")} disabled={!data.in_sample}>
              <span className="radio" />
              <span className="t">Sample (offline)</span>
              <span className="d">
                {data.in_sample
                  ? data.recorded
                    ? "Bundled archive and recorded Nemotron replies. Reproduces the live run in about a second."
                    : "Bundled archive; no recorded replies for this IPO, so model steps are skipped."
                  : "This IPO is not in the bundled sample."}
              </span>
            </button>
            {startError && <ErrorBox error={startError} />}
            <button className="btn btn-primary btn-lg" onClick={start} disabled={starting}>
              {starting ? <Loader2 size={17} className="spin" /> : <Play size={17} />}
              {starting ? "Starting…" : "Run investigation"}
            </button>
            {!rhp && <p className="small muted">No RHP text in the archive: the run rates this IPO on its parties, the watchlist and the banker's record only.</p>}
          </div>
        </Card>
      </div>
    </div>
  );
}

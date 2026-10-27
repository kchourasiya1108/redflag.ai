import { useState } from "react";
import { ArrowRight, ChevronLeft, Eye, EyeOff, Loader2, Search } from "lucide-react";
import { api } from "../api";
import { titleCase } from "./Home";
import { Card, ErrorBox, SeverityBadge, Skeleton } from "../components/ui";
import type { Status } from "../types";
import { Link, RATING_TEXT, date, navigate, odds, pct, rupees, useAsync } from "../lib";

export function IpoPage({ id }: { id: string; status?: Status | null }) {
  const { data, error, loading } = useAsync(() => api.ipo(id), [id]);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [hindsight, setHindsight] = useState(false);

  if (loading && !data) return <div className="container stack"><Skeleton h={34} w={360} /><Skeleton h={220} /></div>;
  if (error) return <div className="container"><ErrorBox error={error} /></div>;
  if (!data) return null;
  const ipo = data.ipo;
  const latest = data.runs.find((r) => r.rating);
  const price = Number(ipo.issue_price), lot = Number(ipo.lot_size);

  const start = async () => {
    setStarting(true);
    setStartError(null);
    try {
      navigate(`/run/${(await api.check(id)).run_id}`);
    } catch (e) {
      const msg = (e as Error).message;
      setStartError(/busy|already/i.test(msg) ? "Another check is running. Please wait a minute and try again." : msg);
      setStarting(false);
    }
  };

  const facts: [string, string][] = ([
    ["Bid opened", date(ipo.open_date as string)],
    ["Price per share", rupees(ipo.issue_price)],
    ["Minimum investment", price && lot ? rupees(price * lot) : "–"],
    ["Issue size", ipo.issue_size_cr ? `₹${Number(ipo.issue_size_cr).toFixed(2)} crore` : "–"],
    ["Exchange", String(ipo.exchange ?? "–")],
    ["Lead manager", ipo.banker_raw ? titleCase(String(ipo.banker_raw)) : "–"],
  ] as [string, string][]).filter(([, v]) => v !== "–");
  const outcome = data.outcome;

  return (
    <div className="container stack-lg narrow">
      <div className="stack-sm">
        <Link to="/" className="small row"><ChevronLeft size={14} /> Home</Link>
        <h1>{titleCase(String(ipo.company))}</h1>
      </div>

      {latest ? (
        <Link to={`/run/${latest.run_id}`} className="card latest-card">
          <SeverityBadge level={latest.rating} size="lg" />
          <div style={{ minWidth: 0 }}>
            <div className="t">{RATING_TEXT[latest.rating!]?.headline}</div>
            <div className="small muted">Chance of a bad outcome: {odds(latest.p_adverse)}</div>
          </div>
          <span className="row small nowrap">Read the report <ArrowRight size={14} /></span>
        </Link>
      ) : (
        <Card>
          <div className="stack">
            {startError && <ErrorBox error={startError} />}
            <button className="btn btn-primary btn-lg" onClick={start} disabled={starting}>
              {starting ? <Loader2 size={17} className="spin" /> : <Search size={17} />} {starting ? "Starting…" : "Check this IPO"}
            </button>
          </div>
        </Card>
      )}

      <Card title="About the issue">
        <div className="kv">
          {facts.map(([k, v]) => <div key={k}><div className="k">{k}</div><div className="v">{v}</div></div>)}
        </div>
      </Card>

      {outcome && (
        <div className="hindsight">
          <div className="between">
            <div>
              <b>What happened after listing</b>
              <div className="small muted">Hidden so you can judge the report first.</div>
            </div>
            <button className="btn btn-sm" onClick={() => setHindsight((h) => !h)}>
              {hindsight ? <EyeOff size={14} /> : <Eye size={14} />} {hindsight ? "Hide" : "Show"}
            </button>
          </div>
          {hindsight && (
            <div className="kv" style={{ marginTop: 12 }}>
              <div><div className="k">Share price after 6 months</div><div className="v">{signed(outcome.ret_6m as number)}</div></div>
              <div><div className="k">Market over the same time</div><div className="v">{signed(outcome.index_ret_6m as number)}</div></div>
              <div><div className="k">Outcome</div><div className="v">{outcome.adverse === 1 ? "Went badly" : outcome.adverse === 0 ? "Did not go badly" : "Unknown"}</div></div>
            </div>
          )}
        </div>
      )}

      {latest && (
        <button className="btn btn-ghost btn-sm" style={{ alignSelf: "flex-start" }} onClick={start} disabled={starting}>
          {starting ? <Loader2 size={14} className="spin" /> : <Search size={14} />} Check it again
        </button>
      )}
    </div>
  );
}

const signed = (x: number | null | undefined) => (x === null || x === undefined ? "–" : `${x > 0 ? "+" : ""}${pct(x)}`);

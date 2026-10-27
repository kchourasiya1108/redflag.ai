import { useEffect, useMemo, useState } from "react";
import {
  AlertOctagon, Check, ChevronLeft, Loader2, RotateCcw,
} from "lucide-react";
import { api } from "../api";
import { Report } from "./Report";
import { Card, ErrorBox, SeverityBadge, Skeleton } from "../components/ui";
import type { CaseFile, Explain, RunState, TraceEvent } from "../types";
import { FLAGS, Link, cx, navigate, useInterval } from "../lib";
import { titleCase } from "./Home";

export const STAGES: { agent: string; name: string; what: string }[] = [
  { agent: "Intake", name: "Intake", what: "Finds the IPO and fixes as_of to the bid date" },
  { agent: "Names", name: "Names", what: "Lead managers, auditor, registrar, market maker" },
  { agent: "BankerTrackRecord", name: "Banker record", what: "How the lead manager's earlier SME IPOs did" },
  { agent: "Watchlist", name: "Watchlist", what: "Every party against SEBI orders before the bid" },
  { agent: "Planner", name: "Planner", what: "Picks LIGHT, STANDARD or FORENSIC depth" },
  { agent: "Extractor", name: "Extractor", what: "Restated financials from the RHP, verified" },
  { agent: "Specialists", name: "Specialists", what: "Paper profits, profit spike, eligibility, concentration, auditor" },
  { agent: "NarrativeReader", name: "Narrative reader", what: "Share capital, peers, objects, related parties (Markovian)" },
  { agent: "Skeptic", name: "Skeptic", what: "Argues both sides of each flag with Nemotron" },
  { agent: "CitationVerifier", name: "Citation check", what: "Re-checks every cited page, query and order" },
  { agent: "Scorer", name: "Scorer", what: "The backtested model, no LLM" },
  { agent: "Evaluator", name: "Evaluator", what: "Coverage check; may send gaps back to the Planner" },
  { agent: "Reporter", name: "Reporter", what: "Report card, case file and checked summary" },
];

const STEP_ACTIONS = new Set(["start", "plan", "delegate_task", "score", "evaluate", "final_synthesis", "escalation_check"]);

export function RunPage({ id }: { id: string }) {
  const [state, setState] = useState<RunState | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [extra, setExtra] = useState<{ cf: CaseFile | null; explain: Explain | null } | null>(null);

  // Load the run; follow its trace live while it runs, else read it whole.
  useEffect(() => {
    let live = true;
    let source: EventSource | null = null;
    setState(null);
    setEvents([]);
    setExtra(null);
    setError(null);
    const finish = () =>
      Promise.all([api.run(id), api.case(id).catch(() => null), api.explain(id).catch(() => null), api.trace(id).catch(() => [])])
        .then(([s, cf, explain, trace]) => {
          if (!live) return;
          setState(s);
          setExtra({ cf, explain });
          if (trace.length) setEvents(trace);
        });
    api.run(id).then((s) => {
      if (!live) return;
      setState(s);
      if (s.status === "running") {
        source = new EventSource(`/api/runs/${id}/events`);
        source.onmessage = (m) => setEvents((ev) => [...ev, JSON.parse(m.data)]);
        source.addEventListener("done", () => {
          source?.close();
          finish();
        });
        source.onerror = () => {
          // The browser reconnects on its own; if the run ended meanwhile, load it.
          api.run(id).then((s2) => {
            if (s2.status !== "running") {
              source?.close();
              finish();
            }
          });
        };
      } else {
        finish();
      }
    }, (e) => live && setError(e));
    return () => {
      live = false;
      source?.close();
    };
  }, [id]);

  if (error) return <div className="container stack"><BackLink /><ErrorBox error={error} /></div>;
  if (!state) return <div className="container stack"><Skeleton h={34} w={360} /><Skeleton h={240} /></div>;

  if (state.status === "running") return <LiveRun state={state} events={events} />;
  if (state.status === "error") return <FailedRun state={state} />;
  if (!state.result || !extra) return <div className="container stack"><Skeleton h={34} w={360} /><Skeleton h={240} /></div>;
  return <Report result={state.result} cf={extra.cf} explain={extra.explain} trace={events} />;
}

function BackLink() {
  return <Link to="/reports" className="small row"><ChevronLeft size={14} /> My reports</Link>;
}

export function useRunStats(events: TraceEvent[]) {
  return useMemo(() => {
    const calls = events.filter((e) => e.agent_name === "ModelGateway" && e.action === "model.call");
    const tokens = calls.reduce((s, e) => s + Number(e.detail?.tokens_in ?? 0) + Number(e.detail?.tokens_out ?? 0), 0);
    return {
      queries: events.filter((e) => e.action === "data.query"),
      calls,
      tokens,
      retries: events.filter((e) => e.agent_name === "ModelGateway" && e.action !== "model.call").length,
      markovian: events.filter((e) => e.action === "markovian").length,
      hmem: events.filter((e) => e.agent_name === "HMEM").length,
      errors: events.filter((e) => e.status === "error" || e.status === "errored"),
      models: [...new Set(calls.map((e) => String(e.detail?.served_model ?? "")).filter(Boolean))],
    };
  }, [events]);
}

/** The agents, grouped into steps an investor can follow. */
const PHASES: { title: string; doing: string; agents: string[] }[] = [
  { title: "Looking up the issue", doing: "Finding the issue and everyone named in it", agents: ["Intake", "Names"] },
  { title: "The lead manager's record", doing: "Checking how the lead manager's earlier SME IPOs did", agents: ["BankerTrackRecord"] },
  { title: "SEBI orders", doing: "Screening every party against SEBI orders before the bid", agents: ["Watchlist"] },
  { title: "The financial statements", doing: "Reading and cross-checking the restated accounts", agents: ["Planner", "Extractor", "Specialists"] },
  { title: "The fine print", doing: "Reading share history, peers, use of money and related parties", agents: ["NarrativeReader"] },
  { title: "Challenging each concern", doing: "Looking for an innocent explanation of every concern in the prospectus", agents: ["Skeptic", "CitationVerifier"] },
  { title: "The rating", doing: "Scoring the risk and writing the report", agents: ["Scorer", "Evaluator", "Reporter"] },
];

function LiveRun({ state, events }: { state: RunState; events: TraceEvent[] }) {
  const [now, setNow] = useState(Date.now() / 1000);
  useInterval(() => setNow(Date.now() / 1000), 500);
  const startedAt = state.started_at ?? (events[0] ? new Date(events[0].timestamp).getTime() / 1000 : now);
  const elapsed = Math.max(0, now - startedAt);
  const [company, setCompany] = useState<string | null>(null);
  useEffect(() => {
    if (state.ipo_id) api.ipo(state.ipo_id).then((d) => setCompany(titleCase(String(d.ipo.company))), () => {});
  }, [state.ipo_id]);
  const lastAt = events.length ? new Date(events[events.length - 1].timestamp).getTime() / 1000 : startedAt;
  const quiet = now - lastAt;

  // Progress from the agents' own trace events; an agent past the last finished one was skipped.
  const done = new Map<string, TraceEvent>();
  for (const e of events) if (STEP_ACTIONS.has(e.action) && e.detail?.elapsed_ms !== undefined) done.set(e.agent_name, e);
  const lastDone = STAGES.reduce((m, s, i) => (done.has(s.agent) ? i : m), -1);
  const finished = (agent: string) => done.has(agent) || STAGES.findIndex((s) => s.agent === agent) < lastDone;
  const phaseState = PHASES.map((ph) => (ph.agents.every(finished) ? "done" : ph.agents.some(finished) ? "active" : "pending"));
  const activeIdx = phaseState.findIndex((s) => s !== "done");
  const flags = [...events.map((e) => e.output_summary).join(" ").matchAll(/\b(F\d{1,2})=(HIGH|ELEVATED|LOW)\b/g)]
    .reduce((m, [, f, s]) => m.set(f, s), new Map<string, string>());
  const concerns = [...flags].filter(([, s]) => s !== "LOW");
  const progress = (phaseState.filter((s) => s === "done").length + (activeIdx >= 0 ? 0.5 : 0)) / PHASES.length;

  return (
    <div className="container stack-lg narrow">
      <div className="stack-sm">
        <BackLink />
        <h1>{company ?? "Checking…"}</h1>
        <p className="muted">
          {state.sample_mode ? "This takes a few seconds." : "This usually takes about 3 minutes. Keep this page open; the report appears here when it is ready."}
        </p>
      </div>

      <Card>
        <div className="stack">
          <div className="between small">
            <span className="muted">{activeIdx >= 0 ? PHASES[activeIdx].doing : "Finishing up"}…</span>
            <span className="faint tabular">{Math.floor(elapsed / 60)}:{String(Math.floor(elapsed % 60)).padStart(2, "0")}</span>
          </div>
          <div className="progress" aria-label={`About ${Math.round(progress * 100)}% done`}><div style={{ width: `${Math.max(4, progress * 100)}%` }} /></div>
          <ol className="phases">
            {PHASES.map((ph, i) => {
              const st = phaseState[i] === "done" ? "done" : i === activeIdx ? "active" : "pending";
              return (
                <li key={ph.title} className={cx("phase", st)}>
                  <span className="icon">
                    {st === "done" ? <Check size={13} strokeWidth={3} /> : st === "active" ? <Loader2 size={13} className="spin" /> : <span className="tiny">{i + 1}</span>}
                  </span>
                  <div>
                    <div className="name">{ph.title}</div>
                    {st === "active" && <div className="what">{ph.doing}</div>}
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      </Card>

      {quiet > 20 && <p className="small faint">Still working: {lastDone < 4 ? "looking up records can take up to a minute." : "long prospectus sections take a minute or more to read."}</p>}

      {concerns.length > 0 && (
        <Card title="Concerns found so far" hint="Some may still be explained away before the report">
          <div className="stack-sm">
            {concerns.map(([f, s]) => (
              <div className="between" key={f}>
                <span className="small"><b>{FLAGS[f]?.title ?? f}</b><div className="faint tiny">{FLAGS[f]?.what}</div></span>
                <SeverityBadge level={s} kind="concern" />
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

function FailedRun({ state }: { state: RunState }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const retry = async (sample: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.start(state.ipo_id!, sample);
      navigate(`/run/${r.run_id}`);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  const dataDown = state.error?.type === "DataSourceUnavailable";
  return (
    <div className="container stack-lg narrow">
      <BackLink />
      <Card>
        <div className="stack">
          <div className="row"><AlertOctagon size={22} color="var(--high)" /><h1 style={{ fontSize: 22 }}>We couldn't finish this check</h1></div>
          <p className="muted">
            {dataDown
              ? "Our data service did not respond, so we stopped rather than rate the IPO on incomplete information."
              : state.error?.recoverable ? "Something went wrong part-way through. This is usually temporary." : "Something went wrong that a retry will not fix."}
          </p>
          {err && <ErrorBox error={err} />}
          <div className="row-wrap">
            {state.error?.recoverable && <button className="btn btn-primary" onClick={() => retry(!!state.sample_mode)} disabled={busy}><RotateCcw size={15} /> Try again</button>}
            {!state.sample_mode && <button className="btn" onClick={() => retry(true)} disabled={busy}>Run a quick offline check</button>}
            <Link to={`/ipo/${state.ipo_id}`} className="btn btn-ghost">Back to the IPO</Link>
          </div>
        </div>
      </Card>
    </div>
  );
}

import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertOctagon, Brain, Check, ChevronLeft, Cpu, Database, Loader2, Minus, RotateCcw, Timer,
} from "lucide-react";
import { api } from "../api";
import { Report } from "./Report";
import { Card, ErrorBox, SeverityBadge, Skeleton, Stat } from "../components/ui";
import type { CaseFile, Explain, RunState, TraceEvent } from "../types";
import { FLAGS, Link, cx, navigate, useInterval } from "../lib";

const TARGET_S = 180;

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

function stageOfModelCall(purpose: string): string {
  const p = purpose.split(":")[0];
  return { extract: "Extractor", narrative: "NarrativeReader", skeptic: "Skeptic", summary: "Reporter", report: "Reporter" }[p] ?? "";
}

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
  if (state.status === "error") return <FailedRun state={state} events={events} />;
  if (!state.result || !extra) return <div className="container stack"><Skeleton h={34} w={360} /><Skeleton h={240} /></div>;
  return <Report result={state.result} cf={extra.cf} explain={extra.explain} trace={events} />;
}

function BackLink() {
  return <Link to="/runs" className="small row"><ChevronLeft size={14} /> Runs</Link>;
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

function LiveRun({ state, events }: { state: RunState; events: TraceEvent[] }) {
  const [now, setNow] = useState(Date.now() / 1000);
  useInterval(() => setNow(Date.now() / 1000), 500);
  const startedAt = state.started_at ?? (events[0] ? new Date(events[0].timestamp).getTime() / 1000 : now);
  const elapsed = Math.max(0, now - startedAt);
  const stats = useRunStats(events);
  const [company, setCompany] = useState<string | null>(null);
  useEffect(() => {
    if (state.ipo_id) api.ipo(state.ipo_id).then((d) => setCompany(String(d.ipo.company)), () => {});
  }, [state.ipo_id]);
  const lastAt = events.length ? new Date(events[events.length - 1].timestamp).getTime() / 1000 : startedAt;
  const quiet = now - lastAt;

  // Stage progress from the agents' own trace events.
  const done = new Map<string, TraceEvent>();
  for (const e of events) if (STEP_ACTIONS.has(e.action) && e.detail?.elapsed_ms !== undefined) done.set(e.agent_name, e);
  const lastDone = STAGES.reduce((m, s, i) => (done.has(s.agent) ? i : m), -1);
  const latest = [...events].reverse().find((e) => e.agent_name !== "Orchestrator");
  const activity: Record<string, string> = {};
  for (const e of events) {
    if (e.action === "model.call") activity[stageOfModelCall(String(e.detail?.purpose ?? ""))] = `Nemotron: ${e.detail?.purpose}`;
    else if (e.action === "extract_attempt") activity.Extractor = e.output_summary;
    else if (e.action === "markovian") activity.NarrativeReader = `Markovian ${e.output_summary}`;
  }
  const flags = [...events.map((e) => e.output_summary).join(" ").matchAll(/\b(F\d{1,2})=(HIGH|ELEVATED|LOW)\b/g)]
    .reduce((m, [, f, s]) => m.set(f, s), new Map<string, string>());

  const feed = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = feed.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 120) el.scrollTop = el.scrollHeight;
  }, [events.length]);

  return (
    <div className="container stack-lg">
      <div className="stack-sm">
        <BackLink />
        <div className="between" style={{ flexWrap: "wrap" }}>
          <div className="stack-sm">
            <div className="row-wrap">
              <span className="pill"><span className="dot dot-live" /> Running</span>
              <span className="pill">{state.sample_mode ? "Sample mode" : "Live · Zetaris + Nightshift"}</span>
              <span className="pill mono">{state.run_id}</span>
            </div>
            <h1>{company ?? state.ipo_id}</h1>
          </div>
        </div>
      </div>

      <div className="grid-4">
        <Stat icon={<Timer size={13} />} label="Elapsed" value={`${Math.floor(elapsed / 60)}:${String(Math.floor(elapsed % 60)).padStart(2, "0")}`}
          sub={elapsed <= TARGET_S ? `target ${TARGET_S / 60} min` : "past the 3 min target; hard limit 10 min"} />
        <Stat icon={<Database size={13} />} label="Zetaris queries" value={stats.queries.length} sub="named templates, leakage-guarded" />
        <Stat icon={<Brain size={13} />} label="Nemotron calls" value={stats.calls.length} sub={`${(stats.tokens / 1000).toFixed(1)}k tokens${stats.retries ? ` · ${stats.retries} retries` : ""}`} />
        <Stat icon={<Cpu size={13} />} label="Engines" value={stats.markovian + stats.hmem} sub={`Markovian ${stats.markovian} · H-MEM ${stats.hmem}`} />
      </div>
      {quiet > 20 && (
        <div className="callout">
          <Loader2 size={18} className="spin" />
          <div>
            No new step for {Math.round(quiet)} s; {STAGES[lastDone + 1]?.name ?? "the run"} is still working.{" "}
            {lastDone < 4
              ? "Early steps wait on Zetaris: a query that gets no answer is retried for up to a minute before the run stops."
              : "Nemotron calls on a large model can take a minute or more each."}
          </div>
        </div>
      )}
      <div className="progress" aria-label="Progress"><div style={{ width: `${((lastDone + 1) / STAGES.length) * 100}%` }} /></div>

      <div className="report-grid" style={{ gridTemplateColumns: "minmax(0, 1fr) 380px" }}>
        <Card title="Live trace" hint="Every agent step, query and model call as it happens" pad={false}>
          <div className="feed" ref={feed} aria-live="polite">
            {events.filter((e) => e.action !== "transition").map((e, i) => (
              <div key={i} className={cx("feed-item", (e.status === "error" || e.status === "errored") && "err")}>
                <span className="ts">{e.timestamp.slice(11, 19)}</span>
                <span className="who">{e.agent_name}</span>
                <span className="msg">{e.output_summary}</span>
              </div>
            ))}
            {!events.length && <div className="empty small"><Loader2 className="spin" size={18} /> Waiting for the first event…</div>}
          </div>
        </Card>

        <div className="stack">
          <Card title="Agents" pad={false}>
            <div className="pipeline" style={{ padding: 8 }}>
              {STAGES.map((s, i) => {
                const ev = done.get(s.agent);
                const status = ev ? "done" : i < lastDone ? "skipped" : i === lastDone + 1 ? "active" : "pending";
                return (
                  <div key={s.agent} className={cx("stage", status)}>
                    <span className="icon">
                      {status === "done" ? <Check size={13} strokeWidth={3} /> : status === "active" ? <Loader2 size={13} className="spin" />
                        : status === "skipped" ? <Minus size={13} /> : <span className="tiny">{i + 1}</span>}
                    </span>
                    <div style={{ minWidth: 0 }}>
                      <div className="name">{s.name}</div>
                      <div className="what">
                        {ev ? ev.output_summary : status === "active" ? activity[s.agent] ?? s.what : status === "skipped" ? "Not needed at this depth" : s.what}
                      </div>
                    </div>
                    <span className="t">{ev?.detail?.elapsed_ms !== undefined ? fmtMs(Number(ev.detail.elapsed_ms)) : ""}</span>
                  </div>
                );
              })}
            </div>
          </Card>
          {flags.size > 0 && (
            <Card title="Flags so far" hint="Before the Skeptic and the score">
              <div className="stack-sm">
                {[...flags].map(([f, s]) => (
                  <div className="between" key={f}>
                    <span className="small"><span className="mono faint">{f}</span> {FLAGS[f]?.title ?? f}</span>
                    <SeverityBadge level={s} />
                  </div>
                ))}
              </div>
            </Card>
          )}
          {latest && <p className="small faint">Latest: {latest.agent_name} · {latest.action}</p>}
        </div>
      </div>
    </div>
  );
}

const fmtMs = (ms: number) => (ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);

function FailedRun({ state, events }: { state: RunState; events: TraceEvent[] }) {
  const [busy, setBusy] = useState(false);
  const retry = async (sample: boolean) => {
    setBusy(true);
    try {
      const r = await api.start(state.ipo_id!, sample);
      navigate(`/run/${r.run_id}`);
    } catch (e) {
      alert((e as Error).message);
      setBusy(false);
    }
  };
  const last = [...events].reverse().find((e) => e.status === "error");
  return (
    <div className="container stack-lg">
      <BackLink />
      <Card>
        <div className="stack">
          <div className="row"><AlertOctagon size={22} color="var(--high)" /><h1 style={{ fontSize: 22 }}>The run stopped</h1></div>
          <div className="callout err">
            <AlertOctagon size={18} />
            <div>
              <b>{state.error?.type}</b>: {state.error?.message}
              {last && <div className="small" style={{ marginTop: 4 }}>at {last.agent_name}</div>}
            </div>
          </div>
          <p className="muted small">
            {state.error?.type === "DataSourceUnavailable"
              ? "Zetaris did not answer. Check that the Linux PC is on and the Zetaris services are running, then retry. Sample mode works offline."
              : state.error?.recoverable ? "This is usually temporary. Retry, or use sample mode." : "Retrying will not help with this error."}
          </p>
          <div className="row-wrap">
            {state.error?.recoverable && <button className="btn btn-primary" onClick={() => retry(!!state.sample_mode)} disabled={busy}><RotateCcw size={15} /> Retry</button>}
            {!state.sample_mode && <button className="btn" onClick={() => retry(true)} disabled={busy}>Run in sample mode</button>}
            <Link to={`/ipo/${state.ipo_id}`} className="btn btn-ghost">Back to the IPO</Link>
          </div>
        </div>
      </Card>
      {events.length > 0 && (
        <Card title="Trace up to the failure" pad={false}>
          <div className="feed">
            {events.filter((e) => e.action !== "transition").map((e, i) => (
              <div key={i} className={cx("feed-item", e.status === "error" && "err")}>
                <span className="ts">{e.timestamp.slice(11, 19)}</span><span className="who">{e.agent_name}</span><span className="msg">{e.output_summary}</span>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

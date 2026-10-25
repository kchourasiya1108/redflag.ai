import { useState, type MouseEvent, type ReactNode } from "react";
import type { CaseFile, Explain, TraceEvent } from "../types";
import { FEATURE_NAMES, num, pct } from "../lib";

// -- hover tooltip ------------------------------------------------------------

export function useTooltip() {
  const [tip, setTip] = useState<{ x: number; y: number; body: ReactNode } | null>(null);
  const bind = (body: ReactNode) => ({
    onMouseMove: (e: MouseEvent) => setTip({ x: e.clientX + 14, y: e.clientY + 14, body }),
    onMouseLeave: () => setTip(null),
  });
  const node = tip && (
    <div className="tooltip" style={{ left: Math.min(tip.x, window.innerWidth - 300), top: tip.y }}>{tip.body}</div>
  );
  return { bind, node };
}

// -- probability on the model's bands -----------------------------------------

export function ProbabilityScale({ p, bands }: { p: number | null; bands: { low_below: number; high_at: number } }) {
  const max = Math.max(0.6, Math.ceil(((p ?? 0) + 0.05) * 10) / 10);
  const at = (x: number) => `${(Math.min(x, max) / max) * 100}%`;
  const ticks = [...Array(Math.round(max * 10) + 1)].map((_, i) => i / 10);
  return (
    <div className="scale" aria-label={`Probability of an adverse outcome ${pct(p)}`}>
      {p !== null && (
        <div className="marker" style={{ left: at(p) }}>
          <span className="lbl">{pct(p)}</span>
          <span className="stem" />
        </div>
      )}
      <div className="track">
        <div className="seg low" style={{ width: at(bands.low_below) }} title={`LOW below ${pct(bands.low_below)}`} />
        <div className="seg elev" style={{ width: `calc(${at(bands.high_at)} - ${at(bands.low_below)})` }} title="ELEVATED" />
        <div className="seg high" style={{ flex: 1 }} title={`HIGH from ${pct(bands.high_at)}`} />
      </div>
      <div className="ticks">
        {ticks.map((t) => <span key={t} style={{ left: at(t) }}>{Math.round(t * 100)}%</span>)}
      </div>
      <div className="bands">
        <span>LOW &lt; {pct(bands.low_below)}</span>
        <span>ELEVATED</span>
        <span>HIGH ≥ {pct(bands.high_at)}</span>
      </div>
    </div>
  );
}

// -- why this probability: each feature's push on the log-odds ----------------

export function Contributions({ explain }: { explain: Explain }) {
  const { bind, node } = useTooltip();
  const rows = [...explain.features].sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));
  const extent = Math.max(0.25, ...rows.map((r) => Math.abs(r.contribution)));
  return (
    <div className="stack">
      <div className="legend">
        <span><i className="sw" style={{ background: "var(--raise)" }} />Raises the risk</span>
        <span><i className="sw" style={{ background: "var(--lower)" }} />Lowers the risk</span>
        <span className="faint">Bar length: change in log-odds · grey label: not known for this IPO (filled with the training median)</span>
      </div>
      <div className="bars" role="table" aria-label="Feature contributions">
        {rows.map((r) => {
          const w = (Math.abs(r.contribution) / extent) * 50;
          const up = r.contribution >= 0;
          return (
            <div className="bar-row" key={r.feature} role="row"
              {...bind(<>
                <b>{FEATURE_NAMES[r.feature] ?? r.feature}</b><br />
                value: {r.missing ? "not known" : num(r.value, 3)}<br />
                log-odds: {r.contribution >= 0 ? "+" : ""}{r.contribution.toFixed(3)}
              </>)}>
              <span className="lab" role="cell" style={r.missing ? { color: "var(--text-3)" } : undefined}>
                {FEATURE_NAMES[r.feature] ?? r.feature}
              </span>
              <div className="bar-track" role="cell">
                <div className="axis" />
                {Math.abs(r.contribution) > 0.0005 && (
                  <div className={`fill ${up ? "up" : "down"}`}
                    style={up ? { left: "50%", width: `${w}%` } : { right: "50%", width: `${w}%` }} />
                )}
              </div>
              <span className="val" role="cell">{r.contribution >= 0 ? "+" : ""}{r.contribution.toFixed(2)}</span>
            </div>
          );
        })}
      </div>
      <p className="small faint">
        z = {explain.intercept.toFixed(2)} (base) {rows.reduce((s, r) => s + r.contribution, 0) >= 0 ? "+" : "−"}{" "}
        {Math.abs(rows.reduce((s, r) => s + r.contribution, 0)).toFixed(2)} (features) → p = {pct(explain.p_adverse)} · model {explain.version}
      </p>
      {node}
    </div>
  );
}

// -- the parties and the orders they are named in ------------------------------

const ROLE: Record<string, string> = {
  banker: "Lead manager", auditor: "Auditor", registrar: "Registrar", market_maker: "Market maker",
  promoter: "Promoter", director: "Director", sebi_order: "SEBI order",
};

export function PartyGraph({ cf, company }: { cf: CaseFile; company: string }) {
  const { bind, node } = useTooltip();
  const parties = cf.entities.filter((e) => e.type !== "sebi_order");
  const orders = cf.entities.filter((e) => e.type === "sebi_order");
  const W = 760, rowH = 64;
  const H = Math.max(parties.length, orders.length, 3) * rowH + 40;
  const pos: Record<string, [number, number]> = {};
  const col = (items: typeof parties, x: number) =>
    items.forEach((e, i) => (pos[e.id] = [x, 20 + (H - 40) * ((i + 0.5) / items.length)]));
  pos["company"] = [110, H / 2];
  col(parties, 380);
  col(orders, 640);
  const short = (s: string, n = 34) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  return (
    <div className="graph">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Parties to the issue and SEBI orders they are named in">
        {cf.edges.map((e, i) => {
          const a = pos[e.src];
          const b = e.dst.startsWith("company:") ? pos["company"] : pos[e.dst];
          if (!a || !b) return null;
          const alert = e.type === "named_in_order";
          const [x1, y1, x2, y2] = alert ? [a[0] + 8, a[1], b[0] - 8, b[1]] : [b[0] + 8, b[1], a[0] - 8, a[1]];
          const mx = (x1 + x2) / 2;
          return (
            <g key={i}>
              <path className={`edge${alert ? " alert" : ""}`} fill="none" d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} />
            </g>
          );
        })}
        <g>
          <circle cx={pos.company[0]} cy={pos.company[1]} r={10} fill="var(--ink)" />
          <text className="node-label" x={pos.company[0]} y={pos.company[1] + 28} textAnchor="middle" style={{ fontWeight: 650 }}>{short(company, 26)}</text>
          <text className="node-sub" x={pos.company[0]} y={pos.company[1] + 43} textAnchor="middle">Issuer</text>
        </g>
        {parties.map((e) => {
          const [x, y] = pos[e.id];
          const linked = cf.edges.some((d) => d.src === e.id && d.type === "named_in_order");
          return (
            <g key={e.id} {...bind(<><b>{e.name}</b><br />{ROLE[e.type] ?? e.type} · from {e.source}</>)} style={{ cursor: "default" }}>
              <rect x={x - 22} y={y - 22} width={44} height={44} fill="transparent" />
              <circle cx={x} cy={y} r={8} fill={linked ? "var(--high)" : "var(--surface)"} stroke={linked ? "var(--surface)" : "var(--text-2)"} strokeWidth={2} />
              <text className="node-label" x={x + 16} y={y - 2}>{short(e.name)}</text>
              <text className="node-sub" x={x + 16} y={y + 13}>{ROLE[e.type] ?? e.type}</text>
            </g>
          );
        })}
        {orders.map((e) => {
          const [x, y] = pos[e.id];
          return (
            <g key={e.id} {...bind(<><b>{e.name}</b><br />{e.source}</>)}>
              <rect x={x - 7} y={y - 7} width={14} height={14} rx={3} fill="var(--high)" />
              <text className="node-label" x={x + 14} y={y - 2}>{short(e.source.replace("sebi:", ""), 16)}</text>
              <text className="node-sub" x={x + 14} y={y + 13}>SEBI order</text>
            </g>
          );
        })}
      </svg>
      {node}
    </div>
  );
}

// -- where the run's time went --------------------------------------------------

export function AgentTimeline({ trace }: { trace: TraceEvent[] }) {
  const { bind, node } = useTooltip();
  if (trace.length < 2) return null;
  const t = (e: TraceEvent) => new Date(e.timestamp).getTime() / 1000;
  const t0 = t(trace[0]);
  const total = Math.max(1, t(trace[trace.length - 1]) - t0);
  // Each agent's step ends at its own trace event and lasts elapsed_ms.
  const steps = trace
    .filter((e) => typeof e.detail?.elapsed_ms === "number" && !["Orchestrator", "DataPort", "ModelGateway", "HMEM"].includes(e.agent_name))
    .map((e) => {
      const dur = (e.detail!.elapsed_ms as number) / 1000;
      const end = t(e) - t0;
      return { agent: e.agent_name, action: e.action, start: Math.max(0, end - dur), dur, summary: e.output_summary };
    });
  return (
    <div className="stack-sm">
      {steps.map((s, i) => (
        <div className="timeline-row" key={i} {...bind(<><b>{s.agent}</b> · {s.action}<br />{s.summary}</>)}>
          <span className="small" style={{ fontWeight: 550 }}>{s.agent}</span>
          <div className="lane">
            <div className="seg" style={{ left: `${(s.start / total) * 100}%`, width: `${Math.max(0.4, (s.dur / total) * 100)}%` }} />
          </div>
          <span className="small mono faint" style={{ textAlign: "right" }}>{s.dur < 1 ? `${Math.round(s.dur * 1000)} ms` : `${s.dur.toFixed(1)} s`}</span>
        </div>
      ))}
      <p className="small faint">Trace timestamps are to the second, so very short steps show as a sliver at their end time.</p>
      {node}
    </div>
  );
}

// -- simple horizontal metric bars (backtest) ----------------------------------

export function MetricBars({ rows, max = 1, format = (v: number) => v.toFixed(3), reference }: {
  rows: { label: string; value: number; highlight?: boolean; note?: string }[];
  max?: number; format?: (v: number) => string; reference?: { value: number; label: string };
}) {
  const { bind, node } = useTooltip();
  return (
    <div className="bars">
      {rows.map((r) => (
        <div className="bar-row" key={r.label} {...bind(<><b>{r.label}</b><br />{format(r.value)}{r.note ? <><br />{r.note}</> : null}</>)}>
          <span className="lab" style={r.highlight ? { color: "var(--text)", fontWeight: 600 } : undefined}>{r.label}</span>
          <div className="bar-track">
            {reference && (
              <div className="axis" style={{ left: `${(reference.value / max) * 100}%`, background: "var(--text-3)" }} title={reference.label} />
            )}
            <div className={`fill ${r.highlight ? "plain" : "muted"}`} style={{ width: `${Math.max(0.5, (r.value / max) * 100)}%` }} />
          </div>
          <span className="val">{format(r.value)}</span>
        </div>
      ))}
      {reference && <div className="small faint">Vertical line: {reference.label}</div>}
      {node}
    </div>
  );
}
